from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """Something a rule detected. `phase=gate` findings can block the proposed action."""

    rule: str
    phase: Literal["gate", "async"]
    step: int
    agent_id: Optional[str] = None
    action_id: Optional[str] = None
    message: str  # human/LLM readable, used as the injected hint
    evidence: Dict[str, Any] = Field(default_factory=dict)
    # Per-finding override of the policy's interrupt setting (e.g. inform a busy
    # agent without stopping it). None = use the policy.
    interrupt: Optional[bool] = None
