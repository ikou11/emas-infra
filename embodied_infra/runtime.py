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
     async findings -> policy -> Commands -> adapter.control()
Every finding / decision / command is written to the infra trace.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from . import events as ev
from .bus import EventBus
from .client import EmasAdapter, EventLike, InfraClient
from .config import InfraConfig, load_config
from .control import Command, ControlAck, GateDecision
from .events import Event, parse_event
from .semantics import validate_table
from .supervisor import Finding, InterventionPolicy, OutcomeInferer, build_rules
from .tools import QueryTools
from .trace import TraceWriter
from .world import InMemoryWorldStore

logger = logging.getLogger("embodied_infra.runtime")

_COMMAND_SUPPORT = {"inject_message": "inject_message", "interrupt": "interrupt",
                    "request_replan": "request_replan"}


class InfraRuntime(InfraClient):
    enabled = True

    def __init__(self, config: Any = None) -> None:
        self.cfg: InfraConfig = load_config(config)
        self.store = InMemoryWorldStore(self.cfg.world_model.allowed_sources)
        self.trace = TraceWriter(self.cfg.trace.dir if self.cfg.trace.enabled else None)
        self.inferer = OutcomeInferer()
        sup = self.cfg.supervisor
        self.rules = build_rules(sup.rules, sup.rule_params) if sup.enabled else []
        iv = self.cfg.interventions
        self.policy = InterventionPolicy(iv.policy, iv.allowed, iv.cooldown_steps,
                                         iv.max_per_agent_episode)
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
        cmds, log = self.policy.decide_async(fs)
        for cmd in cmds:
            ack = self._send(cmd)
            self._record(ev.INFRA_INTERVENTION, {"command": cmd.model_dump(),
                                                 "ack": ack.model_dump()}, cmd.agent_id)
        if log:
            self._record("infra.policy", {"log": log})

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
