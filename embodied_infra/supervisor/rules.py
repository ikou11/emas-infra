"""v1 rule-based supervisor (docs/01-v1-scope.md §4).

Rules never mention EMAS-specific action names: everything comes from the
adapter-provided action semantics table and the world state.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from .. import events as ev
from ..events import Event
from ..semantics import check_all, context_for
from ..world.model import ActionRecord, WorldState
from .findings import Finding


class Rule:
    name = "rule"

    def __init__(self, **params: Any) -> None:
        self.params = params

    def on_event(self, event: Event, world: WorldState) -> List[Finding]:
        return []

    def on_outcome(self, rec: ActionRecord, outcome: str, detail: str,
                   world: WorldState) -> List[Finding]:
        return []

    def on_findings(self, findings: List[Finding], world: WorldState) -> List[Finding]:
        """React to other rules' findings (one level deep: the output is not fed back)."""
        return []

    def reset(self) -> None:
        pass


def _holding_text(agent) -> str:
    if agent is None or not agent.holding_known:
        return "unknown"
    return agent.holding or "nothing"


def _args_text(args: Any) -> str:
    if isinstance(args, dict):
        return ", ".join(f"{k}={v}" for k, v in args.items() if k != "robot")
    return str(args) if args else ""


def _proposal(event: Event, world: WorldState):
    rec = world.actions.get(event.data.get("action_id"))
    agent = world.agents.get(event.agent_id)
    return rec, agent


class UnknownActionRule(Rule):
    name = "unknown_action"

    def on_event(self, event, world):
        if event.type != ev.ACTION_PROPOSED:
            return []
        rec, agent = _proposal(event, world)
        if rec is None or agent is None or not agent.action_space:
            return []
        if rec.proposed_name in agent.action_space:
            return []
        return [Finding(rule=self.name, phase="gate", step=event.step, agent_id=agent.agent_id,
                        action_id=rec.action_id,
                        message=(f"Action '{rec.proposed_name}' is not available to you. "
                                 f"Available actions: {sorted(agent.action_space)}."),
                        evidence={"proposed": rec.proposed_name})]


class UnknownEntityRule(Rule):
    name = "unknown_entity"

    def on_event(self, event, world):
        if event.type != ev.ACTION_PROPOSED:
            return []
        rec, agent = _proposal(event, world)
        if rec is None or agent is None or not world.entities:
            return []
        spec = agent.action_space.get(rec.proposed_name)
        args = rec.proposed_args if isinstance(rec.proposed_args, dict) else {}
        if spec is None:
            return []
        bad = {k: args[k] for k in spec.entity_params()
               if k in args and args[k] not in world.entities}
        if not bad:
            return []
        known = sorted(e for e, s in world.entities.items() if s.type != "robot_entity_type")
        return [Finding(rule=self.name, phase="gate", step=event.step, agent_id=agent.agent_id,
                        action_id=rec.action_id,
                        message=(f"Unknown object(s) {list(bad.values())} in '{rec.proposed_name}'. "
                                 f"Valid object ids: {known}."),
                        evidence={"bad_args": bad})]


class CapabilityRule(Rule):
    name = "capability"

    def on_event(self, event, world):
        if event.type != ev.ACTION_PROPOSED:
            return []
        rec, agent = _proposal(event, world)
        if rec is None or agent is None or agent.capability_tags is None:
            return []
        spec = agent.action_space.get(rec.proposed_name)
        need = (spec.semantics.get("requires") if spec else None) or []
        missing = [t for t in need if t not in agent.capability_tags]
        if not missing:
            return []
        return [Finding(rule=self.name, phase="gate", step=event.step, agent_id=agent.agent_id,
                        action_id=rec.action_id,
                        message=(f"You ({agent.embodiment}) lack the capability {missing} required "
                                 f"by '{rec.proposed_name}'. Ask a teammate that has it."),
                        evidence={"missing": missing, "tags": agent.capability_tags})]


class PreconditionRule(Rule):
    name = "precondition"

    def on_event(self, event, world):
        if event.type != ev.ACTION_PROPOSED:
            return []
        rec, agent = _proposal(event, world)
        if rec is None or agent is None:
            return []
        spec = agent.action_space.get(rec.proposed_name)
        pre = (spec.semantics.get("pre") if spec else None) or []
        if not pre:
            return []
        res = check_all(pre, context_for(world, agent.agent_id, rec.proposed_args))
        if res.ok is not False:
            return []
        holding = _holding_text(agent)
        return [Finding(rule=self.name, phase="gate", step=event.step, agent_id=agent.agent_id,
                        action_id=rec.action_id,
                        message=(f"Cannot '{rec.proposed_name}' now: precondition failed "
                                 f"{res.failed}. You are currently holding: {holding}."),
                        evidence={"failed": res.failed, "holding": holding,
                                  "args": rec.proposed_args})]


class OutcomeFailureRule(Rule):
    name = "outcome_failure"

    def on_outcome(self, rec, outcome, detail, world):
        if outcome != "failure":
            return []
        holding = _holding_text(world.agents.get(rec.agent_id))
        return [Finding(rule=self.name, phase="async", step=world.step, agent_id=rec.agent_id,
                        action_id=rec.action_id,
                        message=(f"Your previous action '{rec.name}({_args_text(rec.args)})' "
                                 f"did NOT succeed ({detail}). You are currently holding: {holding}. "
                                 f"Re-plan your next action accordingly."),
                        evidence={"status": rec.status, "detail": detail, "holding": holding})]


class RepeatFailureRule(Rule):
    name = "repeat_failure"

    def __init__(self, n: int = 2, **params):
        super().__init__(**params)
        self.n = int(n)

    def on_outcome(self, rec, outcome, detail, world):
        if outcome != "failure":
            return []
        # The runtime records rec.outcome before calling rules.
        decided = [a for a in world.agent_actions(rec.agent_id)
                   if a.outcome in ("success", "failure")]
        streak = 0
        for a in reversed(decided):
            if a.outcome == "failure" and a.name == rec.name and a.target == rec.target:
                streak += 1
            else:
                break
        if streak < self.n:
            return []
        return [Finding(rule=self.name, phase="async", step=world.step, agent_id=rec.agent_id,
                        action_id=rec.action_id,
                        message=(f"'{rec.name}' on {rec.target} has failed {streak} times in a row. "
                                 f"Try a different approach or ask another agent for help."),
                        evidence={"streak": streak})]


class IdleDeadlockRule(Rule):
    """All agents idle while the goal is unmet.

    `max_alarms_per_state`: alarms for the same set of unmet goal conditions.
    If the agents were already told and made no progress, repeating the same
    hint only costs LLM calls (seen on EMOS perception tasks the agents cannot
    complete).
    """
    name = "idle_deadlock"

    def __init__(self, max_alarms_per_state: int = 2, **params):
        super().__init__(**params)
        self.max_alarms = int(max_alarms_per_state)
        self._alarms: Dict[frozenset, int] = {}

    def reset(self):
        self._alarms = {}

    def on_event(self, event, world):
        if event.type != ev.ACTION_STARTED or not world.agents:
            return []
        if world.goal_satisfied() is not False:  # satisfied or unknown -> no alarm
            return []
        idle = []
        for agent in world.agents.values():
            rec = world.actions.get(agent.current_action) if agent.current_action else None
            spec = agent.action_space.get(rec.name) if rec else None
            if rec is None or spec is None or not spec.semantics.get("idle"):
                return []
            idle.append(agent.agent_id)
        unmet = world.unmet_goal_conditions()
        key = frozenset(unmet)
        if self.max_alarms > 0 and self._alarms.get(key, 0) >= self.max_alarms:
            return []
        self._alarms[key] = self._alarms.get(key, 0) + 1
        return [Finding(rule=self.name, phase="async", step=event.step, agent_id=a,
                        action_id=world.agents[a].current_action,
                        message=(f"All agents are idle but the task is not finished. "
                                 f"Unmet goal conditions: {unmet}. Decide what you can do next."),
                        evidence={"idle_agents": idle, "unmet": unmet})
                for a in idle]


class StalledActionRule(Rule):
    """An action has been running longer than its declared `max_steps`.

    Needs a time base: any event carries `step`, so periodic `state.snapshot`
    events (or any other agent's activity) are enough to notice a stall.
    """
    name = "stalled_action"

    def __init__(self, **params):
        super().__init__(**params)
        self._fired: set = set()

    def reset(self):
        self._fired = set()

    def on_event(self, event, world):
        out = []
        for agent in world.agents.values():
            rec = world.actions.get(agent.current_action) if agent.current_action else None
            if rec is None or rec.started_step is None or rec.action_id in self._fired:
                continue
            spec = agent.action_space.get(rec.name)
            limit = spec.semantics.get("max_steps") if spec else None
            if not limit or spec.semantics.get("idle"):
                continue
            running = event.step - rec.started_step
            if running <= int(limit):
                continue
            self._fired.add(rec.action_id)
            out.append(Finding(
                rule=self.name, phase="async", step=event.step, agent_id=agent.agent_id,
                action_id=rec.action_id,
                message=(f"Your action '{rec.name}({_args_text(rec.args)})' has been running for "
                         f"{running} steps (normally at most {limit}) and is being stopped; the "
                         f"target may be unreachable for you. Choose a different approach or "
                         f"ask a teammate for help."),
                evidence={"running_steps": running, "max_steps": limit}))
        return out


def _is_idle(agent, world: WorldState) -> bool:
    rec = world.actions.get(agent.current_action) if agent.current_action else None
    if rec is None:
        return True
    spec = agent.action_space.get(rec.name)
    return bool(spec and spec.semantics.get("idle"))


class HelpRoutingRule(Rule):
    """When an agent is stuck, tell idle teammates that are able to do the job.

    Generic: helpers are chosen by the semantics table (`requires`) and the
    declared `capability_tags`; infra does not assign the task, it only routes
    the information "X is stuck on Y and you could do it".
    """
    name = "help_routing"

    def __init__(self, triggers: Optional[List[str]] = None, **params):
        super().__init__(**params)
        self.triggers = set(triggers or ["repeat_failure", "capability", "stalled_action"])
        self._sent: set = set()

    def reset(self):
        self._sent = set()

    def on_findings(self, findings, world):
        out = []
        for f in findings:
            if f.rule not in self.triggers or f.agent_id is None:
                continue
            rec = world.actions.get(f.action_id) if f.action_id else None
            name = (rec.proposed_name if f.phase == "gate" else rec.name) if rec else None
            args = (rec.proposed_args if f.phase == "gate" else rec.args) if rec else None
            requester = world.agents.get(f.agent_id)
            if name is None or requester is None:
                continue
            spec = requester.action_space.get(name)
            need = (spec.semantics.get("requires") if spec else None) or []
            for helper in world.agents.values():
                if helper.agent_id == f.agent_id or name not in helper.action_space:
                    continue
                if need and (helper.capability_tags is None
                             or any(t not in helper.capability_tags for t in need)):
                    continue
                if not _is_idle(helper, world):
                    continue
                key = (f.agent_id, name, _args_text(args), helper.agent_id)
                if key in self._sent:
                    continue
                self._sent.add(key)
                unmet = world.unmet_goal_conditions()
                out.append(Finding(
                    rule=self.name, phase="async", step=f.step, agent_id=helper.agent_id,
                    action_id=f.action_id,
                    message=(f"Teammate {f.agent_id} ({requester.embodiment}) is stuck: cannot "
                             f"complete '{name}({_args_text(args)})' ({f.rule}). You are idle "
                             f"and able to do '{name}'. Consider taking over this part of the task"
                             + (f"; unmet goal conditions: {unmet}." if unmet else ".")),
                    evidence={"requester": f.agent_id, "trigger": f.rule, "action": name,
                              "args": args, "requires": need}))
        return out


RULES: Dict[str, Type[Rule]] = {r.name: r for r in [
    UnknownActionRule, UnknownEntityRule, CapabilityRule, PreconditionRule,
    OutcomeFailureRule, RepeatFailureRule, IdleDeadlockRule,
    StalledActionRule, HelpRoutingRule,
]}


def build_rules(names: List[str], params: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Rule]:
    params = params or {}
    out = []
    for n in names:
        if n not in RULES:
            raise ValueError(f"unknown rule {n!r}; available: {sorted(RULES)}")
        out.append(RULES[n](**params.get(n, {})))
    return out
