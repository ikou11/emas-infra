"""Northbound read-only query tools (docs/02-v1-interface-spec.md §6).

Tool definitions are pydantic models, exported as OpenAI function-calling
schemas. Results are JSON strings meant to be read by an LLM.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel, Field

from .world.model import WorldState


class GetEntityArgs(BaseModel):
    entity_id: str = Field(description="Object id exactly as used in action arguments, e.g. 'any_targets|0'.")


class FindEntitiesArgs(BaseModel):
    type: Optional[str] = Field(default=None, description="Filter by entity type.")
    name_contains: Optional[str] = Field(default=None, description="Substring of the id or an alias.")
    near_agent: Optional[str] = Field(default=None, description="Only entities within `radius` of this agent.")
    radius: float = Field(default=2.0, description="Radius in meters for near_agent.")


class GetAgentStateArgs(BaseModel):
    agent_id: str = Field(description="Agent id, e.g. 'agent_0'.")


class ActionHistoryArgs(BaseModel):
    agent_id: Optional[str] = Field(default=None, description="Only actions of this agent.")
    entity_id: Optional[str] = Field(default=None, description="Only actions targeting this object.")
    last_n: int = Field(default=10, description="Maximum number of records, most recent last.")


def _entity_view(world: WorldState, eid: str) -> Dict[str, Any]:
    e = world.entities[eid]
    return {"entity_id": eid, "type": e.type, "aliases": e.aliases, "pos": e.pos,
            "held_by": e.holder, "last_updated_step": e.updated_step, "source": e.source}


def _dist(a, b) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


class QueryTools:
    def __init__(self, world_getter: Callable[[], WorldState]) -> None:
        self._world = world_getter
        self._tools: Dict[str, Tuple[str, Type[BaseModel], Callable[[BaseModel], Any]]] = {
            "world_get_entity": (
                "Get the latest known state of one object (position, who holds it, when it was last observed).",
                GetEntityArgs, self._get_entity),
            "world_find_entities": (
                "List known objects, optionally filtered by type, name or distance to an agent.",
                FindEntitiesArgs, self._find_entities),
            "world_get_agent_state": (
                "Get an agent's latest known position, held object and current action.",
                GetAgentStateArgs, self._get_agent_state),
            "memory_get_action_history": (
                "Get recent executed actions with their status and inferred outcome (success/failure).",
                ActionHistoryArgs, self._action_history),
        }

    def names(self) -> List[str]:
        return list(self._tools)

    def schemas(self) -> List[Dict[str, Any]]:
        out = []
        for name, (desc, model, _) in self._tools.items():
            params = model.model_json_schema()
            params.pop("title", None)
            out.append({"type": "function",
                        "function": {"name": name, "description": desc, "parameters": params}})
        return out

    def call(self, name: str, args: Dict[str, Any]) -> str:
        if name not in self._tools:
            return json.dumps({"error": f"unknown tool {name}", "available": self.names()})
        _, model, fn = self._tools[name]
        try:
            parsed = model.model_validate(args or {})
            return json.dumps(fn(parsed), ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": repr(e)})

    # ----------------------------------------------------------- handlers
    def _get_entity(self, a: GetEntityArgs):
        w = self._world()
        if a.entity_id not in w.entities:
            return {"error": f"unknown entity {a.entity_id}", "known": sorted(w.entities)}
        return _entity_view(w, a.entity_id)

    def _find_entities(self, a: FindEntitiesArgs):
        w = self._world()
        center = None
        if a.near_agent:
            ag = w.agents.get(a.near_agent)
            center = ag.pos if ag else None
        out = []
        for eid, e in w.entities.items():
            if a.type and e.type != a.type:
                continue
            if a.name_contains and a.name_contains not in eid and \
                    not any(a.name_contains in x for x in e.aliases):
                continue
            if center is not None and (e.pos is None or _dist(center, e.pos) > a.radius):
                continue
            out.append(_entity_view(w, eid))
        return {"entities": out}

    def _get_agent_state(self, a: GetAgentStateArgs):
        w = self._world()
        ag = w.agents.get(a.agent_id)
        if ag is None:
            return {"error": f"unknown agent {a.agent_id}", "known": sorted(w.agents)}
        cur = w.actions.get(ag.current_action) if ag.current_action else None
        return {"agent_id": ag.agent_id, "embodiment": ag.embodiment, "pos": ag.pos,
                "holding": ag.holding if ag.holding_known else "unknown",
                "current_action": {"name": cur.name, "args": cur.args, "since_step": cur.started_step}
                if cur else None,
                "assigned_subtask": ag.subtask, "last_updated_step": ag.updated_step}

    def _action_history(self, a: ActionHistoryArgs):
        w = self._world()
        recs = [w.actions[i] for i in w.action_order]
        if a.agent_id:
            recs = [r for r in recs if r.agent_id == a.agent_id]
        if a.entity_id:
            recs = [r for r in recs if isinstance(r.args, dict) and a.entity_id in r.args.values()]
        recs = recs[-max(1, a.last_n):]
        return {"actions": [{"agent_id": r.agent_id, "name": r.name, "args": r.args,
                             "started_step": r.started_step, "ended_step": r.ended_step,
                             "status": r.status, "outcome": r.outcome} for r in recs]}
