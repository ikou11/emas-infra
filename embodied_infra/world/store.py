"""World store: interface + in-memory implementation (v1).

`apply(event)` folds one adapter event into the state. A SQLite implementation can
be added later behind the same interface without touching rules or tools.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .. import events as ev
from ..events import Event
from .model import ActionRecord, ActionSpec, AgentState, EntityState, WorldState


class WorldStore(ABC):
    @abstractmethod
    def apply(self, event: Event) -> None: ...

    @property
    @abstractmethod
    def state(self) -> WorldState: ...

    def set_outcome(self, action_id: str, outcome: str, detail: str = "") -> None:
        rec = self.state.actions.get(action_id)
        if rec is not None:
            rec.outcome, rec.outcome_detail = outcome, detail


def _arg_dict(args: Any) -> Any:
    return dict(args) if isinstance(args, dict) else args


class InMemoryWorldStore(WorldStore):
    def __init__(self, allowed_sources: Optional[List[str]] = None) -> None:
        self._state = WorldState()
        self.allowed_sources = set(allowed_sources) if allowed_sources else None

    @property
    def state(self) -> WorldState:
        return self._state

    def _source_ok(self, source: str) -> bool:
        return self.allowed_sources is None or source in self.allowed_sources

    # ----------------------------------------------------------------- fold
    def apply(self, event: Event) -> None:
        s = self._state
        if event.type == ev.EPISODE_STARTED:
            self._state = s = WorldState(episode_id=event.episode_id)
        s.step = max(s.step, event.step)
        handler = getattr(self, "_on_" + event.type.replace(".", "_"), None)
        if handler is not None:
            handler(event, s)

    def _on_episode_started(self, e: Event, s: WorldState) -> None:
        d = e.data
        s.task_text = d.get("task_text", "")
        s.scene_text = d.get("scene_text", "") or ""
        goal = d.get("goal") or {}
        s.goal_conditions = goal.get("conditions")
        if d.get("max_steps") is not None:
            s.max_steps = int(d["max_steps"])
        for ent in d.get("entities", []):
            s.entities[ent["entity_id"]] = EntityState(
                entity_id=ent["entity_id"], type=ent.get("type"),
                aliases=ent.get("aliases") or [])

    def _on_agent_registered(self, e: Event, s: WorldState) -> None:
        d = e.data
        specs = {}
        for a in d.get("action_space", []):
            spec = ActionSpec(name=a["name"], params=a.get("params") or {},
                              semantics=a.get("semantics") or {})
            specs[spec.name] = spec
        s.agents[e.agent_id] = AgentState(
            agent_id=e.agent_id, embodiment=d.get("embodiment"),
            capabilities=d.get("capabilities") or {},
            capability_tags=d.get("capability_tags"),
            action_space=specs, supports=d.get("supports") or [])

    def _on_plan_assigned(self, e: Event, s: WorldState) -> None:
        s.plan = dict(e.data.get("assignments") or {})
        for agent_id, sub in s.plan.items():
            if agent_id in s.agents:
                s.agents[agent_id].subtask = sub

    def _on_action_proposed(self, e: Event, s: WorldState) -> None:
        d = e.data
        rec = ActionRecord(action_id=d["action_id"], agent_id=e.agent_id,
                           proposed_name=d.get("name"), proposed_args=_arg_dict(d.get("args")),
                           name=d.get("name"), args=_arg_dict(d.get("args")),
                           proposed_step=e.step)
        s.actions[rec.action_id] = rec
        s.action_order.append(rec.action_id)

    def _on_action_started(self, e: Event, s: WorldState) -> None:
        d = e.data
        rec = s.actions.get(d["action_id"])
        if rec is None:
            return
        rec.name, rec.args = d.get("name"), _arg_dict(d.get("args"))
        rec.started_step, rec.gate = e.step, d.get("gate")
        rec.emas_note = d.get("emas_note")
        if e.agent_id in s.agents:
            s.agents[e.agent_id].current_action = rec.action_id

    def _on_action_ended(self, e: Event, s: WorldState) -> None:
        d = e.data
        rec = s.actions.get(d["action_id"])
        if rec is None:
            return
        rec.ended_step, rec.status = e.step, d.get("status")
        if d.get("outcome") and d["outcome"] != "unknown":
            rec.outcome = d["outcome"]  # adapter-provided outcome wins
        agent = s.agents.get(e.agent_id)
        if agent and agent.current_action == rec.action_id:
            agent.current_action = None

    def _on_state_snapshot(self, e: Event, s: WorldState) -> None:
        if not self._source_ok(e.source):
            return
        d = e.data
        for agent_id, a in (d.get("agents") or {}).items():
            agent = s.agents.setdefault(agent_id, AgentState(agent_id=agent_id))
            agent.pos, agent.yaw = a.get("pos"), a.get("yaw")
            if "holding" in a:
                agent.holding, agent.holding_known = a.get("holding"), True
            agent.updated_step = e.step
        for ent_id, rec in (d.get("entities") or {}).items():
            ent = s.entities.setdefault(ent_id, EntityState(entity_id=ent_id))
            ent.pos, ent.updated_step, ent.source = rec.get("pos"), e.step, e.source
            ent.holder = None
        for agent in s.agents.values():
            if agent.holding and agent.holding in s.entities:
                s.entities[agent.holding].holder = agent.agent_id
        if "predicates" in d:
            new = set(d.get("predicates") or [])
            old = s.predicates or set()
            for p in new - old:
                s.predicate_history.setdefault(p, []).append([e.step, None])
            for p in old - new:
                hist = s.predicate_history.get(p)
                if hist and hist[-1][1] is None:
                    hist[-1][1] = e.step
            s.predicates = new
        s.last_snapshot_step = e.step

    def _on_agent_message(self, e: Event, s: WorldState) -> None:
        s.messages.append({"step": e.step, "from": e.agent_id, **e.data})

    def _on_episode_ended(self, e: Event, s: WorldState) -> None:
        s.ended, s.success = True, e.data.get("success")
