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
    name = "idle_deadlock"

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
        return [Finding(rule=self.name, phase="async", step=event.step, agent_id=a,
                        action_id=world.agents[a].current_action,
                        message=(f"All agents are idle but the task is not finished. "
                                 f"Unmet goal conditions: {unmet}. Decide what you can do next."),
                        evidence={"idle_agents": idle, "unmet": unmet})
                for a in idle]


RULES: Dict[str, Type[Rule]] = {r.name: r for r in [
    UnknownActionRule, UnknownEntityRule, CapabilityRule, PreconditionRule,
    OutcomeFailureRule, RepeatFailureRule, IdleDeadlockRule,
]}


def build_rules(names: List[str], params: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Rule]:
    params = params or {}
    out = []
    for n in names:
        if n not in RULES:
            raise ValueError(f"unknown rule {n!r}; available: {sorted(RULES)}")
        out.append(RULES[n](**params.get(n, {})))
    return out
