"""InfraRuntime: the in-process implementation of `InfraClient`.

Processing order for every adapter event (synchronous, deterministic):
  1. trace the raw event            (input trace)
  2. fold it into the world store   (event sourcing)
  3. supervisor:
       action.proposed -> gate rules -> GateDecision (kept until `gate()` is called)
       action.ended    -> remember for outcome inference
       state.snapshot  -> infer outcomes from `post` conditions -> outcome rules
       any event       -> event rules (e.g. idle_deadlock, stalled_action)
     findings        -> finding rules (help_routing), one level deep
     async findings -> policy (cooldown/budget) -> rule-template Commands
                       escalated rules -> LLM decides the Commands (template = fallback)
     plan.assigned   -> optional LLM plan review (assist layer)
   -> adapter.control()
Every finding / decision / command is written to the infra trace.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from . import events as ev
from .bus import EventBus
from .client import EmasAdapter, EventLike, InfraClient
from .config import SAFETY_RULES, InfraConfig, load_config
from .control import Command, ControlAck, GateDecision
from .events import Event, parse_event
from .semantics import validate_table
from .supervisor import Finding, InterventionPolicy, OutcomeInferer, build_rules
from .supervisor.llm import ChatFn, LLMSupervisor, openai_chat_fn
from .tools import QueryTools
from .trace import TraceWriter
from .world import InMemoryWorldStore

logger = logging.getLogger("embodied_infra.runtime")

_COMMAND_SUPPORT = {"inject_message": "inject_message", "interrupt": "interrupt",
                    "request_replan": "request_replan"}


class InfraRuntime(InfraClient):
    enabled = True

    def __init__(self, config: Any = None, llm_chat: Optional[ChatFn] = None) -> None:
        """`llm_chat` injects the supervisor LLM (tests); by default it is built
        from the supervisor env file when `supervisor.llm.enabled`."""
        self.cfg: InfraConfig = load_config(config)
        self.store = InMemoryWorldStore(self.cfg.world_model.allowed_sources)
        self.trace = TraceWriter(self.cfg.trace.dir if self.cfg.trace.enabled else None)
        self.inferer = OutcomeInferer()
        sup = self.cfg.supervisor
        names, params = self._rule_plan()
        self.rules = build_rules(names, params)
        iv = self.cfg.interventions
        self.policy = InterventionPolicy(iv.policy, iv.allowed, iv.cooldown_steps,
                                         iv.max_per_agent_episode)
        chat = None
        if sup.enabled and sup.llm.enabled:
            chat = llm_chat or openai_chat_fn(sup.llm.env_file, sup.llm.model, sup.llm.timeout_s,
                                              sup.llm.temperature, sup.llm.max_tokens,
                                              sup.llm.tool_choice)
        self.llm = LLMSupervisor(chat, sup.llm.max_calls_per_episode, sup.llm.min_interval_steps,
                                 [a for a in iv.allowed if a in ("inject_message", "interrupt")])
        self.tools = QueryTools(lambda: self.store.state)
        self.adapter: Optional[EmasAdapter] = None
        self._decisions: Dict[str, GateDecision] = {}
        # In-memory records (handy for tests and the replay CLI).
        self.findings: List[Finding] = []
        self.records: List[Dict[str, Any]] = []

        self.bus = EventBus()
        self.bus.subscribe(self.trace.write_input, name="trace")
        self.bus.subscribe(self.store.apply, name="world")
        self.bus.subscribe(self._supervise, name="supervisor")

    def _rule_plan(self):
        """Safety rules always; correction rules when the supervisor is on; assist
        rules/options as configured."""
        sup = self.cfg.supervisor
        names = list(SAFETY_RULES)
        params = {k: dict(v) for k, v in sup.rule_params.items()}
        if sup.enabled:
            names += [r for r in sup.rules if r not in names]
            if sup.assist.workload_balance and "workload_risk" not in names:
                names.append("workload_risk")
            if sup.assist.first_failure_help:
                params.setdefault("help_routing", {})["on_first_failure"] = True
        return names, params

    @property
    def world(self):
        return self.store.state

    # ------------------------------------------------------------ contract
    def attach(self, adapter: EmasAdapter) -> None:
        self.adapter = adapter

    def emit(self, event: EventLike) -> None:
        try:
            e = event if isinstance(event, Event) else parse_event(event)
        except Exception as err:
            logger.warning("infra: dropping malformed event: %r", err)
            return
        self.bus.publish(e)

    def gate(self, event: EventLike, timeout_s: float = 2.0) -> GateDecision:
        try:
            e = event if isinstance(event, Event) else parse_event(event)
            aid = e.data.get("action_id")
            if aid not in self._decisions and aid not in self.world.actions:
                self.emit(e)  # adapter called gate() without emit(): process it now
            return self._decisions.pop(aid, GateDecision.allow())
        except Exception as err:  # infra failure must never block the EMAS
            logger.warning("infra: gate failed, allowing: %r", err)
            return GateDecision.allow()

    def tool_schemas(self, agent_id: str) -> List[Dict[str, Any]]:
        return self.tools.schemas() if self.cfg.tools.enabled else []

    def call_tool(self, agent_id: str, name: str, args: Dict[str, Any]) -> str:
        result = self.tools.call(name, args)
        self._record(ev.INFRA_TOOL_CALLED, {"tool": name, "args": args, "result": result},
                     agent_id)
        return result

    def close(self) -> None:
        self.trace.close()

    # ----------------------------------------------------------- internals
    def _run_rule(self, rule, method: str, *args) -> List[Finding]:
        """One failing rule must not silence the others."""
        try:
            return list(getattr(rule, method)(*args))
        except Exception as err:
            msg = f"rule {rule.name}.{method} failed: {err!r}"
            logger.warning(msg)
            self.bus.errors.append(msg)
            return []

    def _record(self, type_: str, data: Dict[str, Any], agent_id: Optional[str] = None):
        rec = self.trace.write_infra(type_, self.world.step, data, agent_id)
        self.records.append(rec)

    def _supervise(self, e: Event) -> None:
        w = self.world
        if e.type == ev.EPISODE_STARTED:
            self.inferer.reset()
            self.policy.reset()
            self.llm.reset()
            self._decisions.clear()
            for r in self.rules:
                r.reset()
        elif e.type == ev.AGENT_REGISTERED:
            agent = w.agents.get(e.agent_id)
            errs = validate_table(agent.action_space) if agent else []
            if errs:
                self._record("infra.warning", {"semantics_errors": errs}, e.agent_id)
            if agent and not any(s.semantics for s in agent.action_space.values()):
                self._record("infra.warning", {"msg": "agent registered without action semantics; "
                                                      "precondition/outcome rules are inactive"},
                             e.agent_id)
        elif e.type == ev.ACTION_PROPOSED:
            self._gate_rules(e)
        elif e.type == ev.ACTION_ENDED:
            self.inferer.on_action_ended(e.data.get("action_id"))
        elif e.type == ev.STATE_SNAPSHOT:
            self._infer_outcomes()
        elif e.type == ev.PLAN_ASSIGNED:
            self._plan_review()
        elif e.type == ev.CONTROL_ACK:
            self._record("control.ack.received", dict(e.data), e.agent_id)
        elif e.type == ev.EPISODE_ENDED:
            self.inferer.reset()  # no post-episode snapshot: outcomes stay unknown

        async_findings: List[Finding] = []
        for r in self.rules:
            async_findings += [f for f in self._run_rule(r, "on_event", e, w) if f.phase == "async"]
        self._dispatch_async(async_findings)

    def _gate_rules(self, e: Event) -> None:
        fs: List[Finding] = []
        for r in self.rules:
            fs += [f for f in self._run_rule(r, "on_event", e, self.world) if f.phase == "gate"]
        for f in fs:
            self.findings.append(f)
            self._record(ev.INFRA_FINDING, f.model_dump(), f.agent_id)
        decision, log = self.policy.decide_gate(fs)
        self._dispatch_async(self._derived(fs), derive=False)
        aid = e.data.get("action_id")
        self._decisions[aid] = decision
        if fs or decision.decision != "allow":
            self._record(ev.INFRA_GATE, {"action_id": aid, "decision": decision.model_dump(),
                                         "policy_log": log}, e.agent_id)

    def _infer_outcomes(self) -> None:
        fs: List[Finding] = []
        for rec, outcome, detail in self.inferer.on_snapshot(self.world):
            self.store.set_outcome(rec.action_id, outcome, detail)
            self._record(ev.INFRA_OUTCOME, {"action_id": rec.action_id, "name": rec.name,
                                            "status": rec.status, "outcome": outcome,
                                            "detail": detail}, rec.agent_id)
            for r in self.rules:
                fs += self._run_rule(r, "on_outcome", rec, outcome, detail, self.world)
        self._dispatch_async(fs)

    def _derived(self, fs: List[Finding]) -> List[Finding]:
        out: List[Finding] = []
        if fs:
            for r in self.rules:
                out += self._run_rule(r, "on_findings", fs, self.world)
        return out

    def _dispatch_async(self, fs: List[Finding], derive: bool = True) -> None:
        if derive:
            fs = fs + self._derived(fs)
        if not fs:
            return
        for f in fs:
            self.findings.append(f)
            self._record(ev.INFRA_FINDING, f.model_dump(), f.agent_id)
        admitted, log = self.policy.admit_async(fs)
        llm_cfg = self.cfg.supervisor.llm
        cmds = self.policy.commands_for(admitted)
        llm_cmds = None
        if self.llm.available and any(f.rule in set(llm_cfg.escalate) for f in admitted):
            llm_cmds, rec = self.llm.decide(self.world, admitted, cmds)
            self._record("supervisor.llm_decision", rec)
        if llm_cmds is not None:
            cmds = llm_cmds
        else:  # rule fallback: drop suggestions that need the LLM's confirmation
            need = set(llm_cfg.require_confirmation)
            risky = [f for f in admitted
                     if f.rule in need and not f.evidence.get("blocked_owner")]
            if risky:
                cmds = self.policy.commands_for([f for f in admitted if f not in risky])
                log += [{"rule": f.rule, "agent_id": f.agent_id, "action": "none",
                         "reason": "needs llm confirmation"} for f in risky]
                admitted = [f for f in admitted if f not in risky]
        for f in admitted:
            log.append({"rule": f.rule, "agent_id": f.agent_id, "action": "admitted"})
        self._issue(cmds)
        if log:
            self._record("infra.policy", {"log": log})

    def _plan_review(self) -> None:
        sup = self.cfg.supervisor
        if not (sup.enabled and sup.assist.plan_review and self.llm.available):
            return
        cmds, rec = self.llm.decide(self.world, [], [], mode="plan_review")
        self._record("supervisor.llm_decision", rec)
        # plan review only advises; it never interrupts
        self._issue([c for c in (cmds or []) if c.type == "inject_message"])

    def _issue(self, cmds: List[Command]) -> None:
        seen = set()
        for cmd in cmds:
            key = (cmd.type, cmd.agent_id, cmd.payload.get("text"))
            if key in seen:
                continue
            seen.add(key)
            ack = self._send(cmd)
            self._record(ev.INFRA_INTERVENTION, {"command": cmd.model_dump(),
                                                 "ack": ack.model_dump()}, cmd.agent_id)

    def _send(self, cmd: Command) -> ControlAck:
        step = self.world.step
        if self.adapter is None:
            return ControlAck(command_id=cmd.command_id, status="ignored", step=step,
                              detail="no adapter attached")
        agent = self.world.agents.get(cmd.agent_id) if cmd.agent_id else None
        if agent is not None and _COMMAND_SUPPORT[cmd.type] not in agent.supports:
            return ControlAck(command_id=cmd.command_id, status="unsupported", step=step,
                              detail=f"{cmd.agent_id} does not declare '{cmd.type}'")
        try:
            return self.adapter.control(cmd)
        except Exception as err:
            return ControlAck(command_id=cmd.command_id, status="failed", step=step,
                              detail=repr(err))


def make_client(config: Any = None) -> InfraClient:
    """Factory used by adapters: returns a NullInfraClient when infra is disabled."""
    from .client import NullInfraClient

    cfg = load_config(config)
    return InfraRuntime(cfg) if cfg.enabled else NullInfraClient()
