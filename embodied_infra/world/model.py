"""In-memory world state, built by folding adapter events (event sourcing).

Everything here is derived from events; replaying the same trace always yields
the same state. Facts carry the step at which they were observed and their
information source, so queries can answer "when" and "how do we know".
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field


class ActionSpec(BaseModel):
    name: str
    params: Dict[str, Any] = Field(default_factory=dict)
    semantics: Dict[str, Any] = Field(default_factory=dict)

    def entity_params(self) -> List[str]:
        return [k for k, v in self.params.items() if v == "entity_id"]


class EntityState(BaseModel):
    entity_id: str
    type: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    pos: Optional[List[float]] = None
    holder: Optional[str] = None
    updated_step: Optional[int] = None
    source: Optional[str] = None


class AgentState(BaseModel):
    agent_id: str
    embodiment: Optional[str] = None
    capabilities: Dict[str, Any] = Field(default_factory=dict)
    capability_tags: Optional[List[str]] = None  # None = not declared by adapter
    action_space: Dict[str, ActionSpec] = Field(default_factory=dict)
    supports: List[str] = Field(default_factory=list)
    pos: Optional[List[float]] = None
    yaw: Optional[float] = None
    holding: Optional[str] = None
    holding_known: bool = False
    current_action: Optional[str] = None  # action_id
    subtask: Optional[str] = None
    updated_step: Optional[int] = None


class ActionRecord(BaseModel):
    action_id: str
    agent_id: str
    name: Optional[str] = None
    args: Any = None
    proposed_name: Optional[str] = None
    proposed_args: Any = None
    proposed_step: Optional[int] = None
    started_step: Optional[int] = None
    ended_step: Optional[int] = None
    status: Optional[str] = None       # completed|timeout|interrupted|rejected|error
    outcome: Optional[str] = None      # success|failure|unknown|n/a (inferred by infra)
    outcome_detail: Optional[str] = None
    gate: Optional[str] = None
    emas_note: Optional[str] = None

    @property
    def target(self) -> Optional[str]:
        args = self.args if isinstance(self.args, dict) else {}
        return args.get("target_obj")


class WorldState(BaseModel):
    episode_id: Optional[str] = None
    step: int = 0
    task_text: str = ""
    scene_text: str = ""
    goal_conditions: Optional[List[str]] = None
    max_steps: Optional[int] = None  # episode step budget, if the adapter reports it
    entities: Dict[str, EntityState] = Field(default_factory=dict)
    agents: Dict[str, AgentState] = Field(default_factory=dict)
    actions: Dict[str, ActionRecord] = Field(default_factory=dict)
    action_order: List[str] = Field(default_factory=list)
    predicates: Optional[Set[str]] = None  # None = never observed
    # predicate -> list of [start_step, end_step or None]
    predicate_history: Dict[str, List[List[Optional[int]]]] = Field(default_factory=dict)
    last_snapshot_step: Optional[int] = None
    ended: bool = False
    success: Optional[bool] = None
    plan: Dict[str, str] = Field(default_factory=dict)
    messages: List[Dict[str, Any]] = Field(default_factory=list)

    # ---------------------------------------------------------------- queries
    def goal_satisfied(self) -> Optional[bool]:
        if not self.goal_conditions or self.predicates is None:
            return None
        return all(c in self.predicates for c in self.goal_conditions)

    def unmet_goal_conditions(self) -> List[str]:
        if not self.goal_conditions or self.predicates is None:
            return []
        return [c for c in self.goal_conditions if c not in self.predicates]

    def agent_actions(self, agent_id: str) -> List[ActionRecord]:
        return [self.actions[a] for a in self.action_order if self.actions[a].agent_id == agent_id]

    def predicate_intervals(self, pred: str) -> List[Tuple[Optional[int], Optional[int]]]:
        return [tuple(x) for x in self.predicate_history.get(pred, [])]
