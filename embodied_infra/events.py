"""Event envelope and helpers (docs/02-v1-interface-spec.md §2-3)."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_PREFIX = "embodied-infra/event/0."
SCHEMA = "embodied-infra/event/0.2"

# Event types emitted by an EMAS adapter.
EPISODE_STARTED = "episode.started"
AGENT_REGISTERED = "agent.registered"
PLAN_ASSIGNED = "plan.assigned"
ACTION_PROPOSED = "action.proposed"
ACTION_STARTED = "action.started"
ACTION_ENDED = "action.ended"
STATE_SNAPSHOT = "state.snapshot"
AGENT_MESSAGE = "agent.message"
LLM_USAGE = "llm.usage"
CONTROL_ACK = "control.ack"
EPISODE_ENDED = "episode.ended"

# Records produced by infra itself (written to the infra output trace).
INFRA_FINDING = "infra.finding"
INFRA_OUTCOME = "action.outcome_inferred"
INFRA_GATE = "infra.gate_decision"
INFRA_INTERVENTION = "intervention.issued"
INFRA_TOOL_CALLED = "tool.called"


class Event(BaseModel):
    """Envelope shared by all events. Unknown fields are kept (forward compat)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_: str = Field(default=SCHEMA, alias="schema")
    seq: int = 0
    session_id: Optional[str] = None
    episode_id: Optional[str] = None
    step: int = 0
    t_wall: float = Field(default_factory=time.time)
    type: str
    agent_id: Optional[str] = None
    source: str = "system"
    data: Dict[str, Any] = Field(default_factory=dict)

    def to_json_dict(self) -> Dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


def parse_event(obj: Dict[str, Any]) -> Event:
    return Event.model_validate(obj)
