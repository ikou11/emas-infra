"""Southbound control contract (docs/02-v1-interface-spec.md §4-5)."""
from __future__ import annotations

import itertools
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

_cmd_counter = itertools.count(1)


class GateDecision(BaseModel):
    decision: Literal["allow", "deny", "replace"] = "allow"
    reason: str = ""
    replacement: Optional[Dict[str, Any]] = None  # {"name": ..., "args": {...}}
    message: Optional[str] = None  # hint to inject into the agent's next decision
    findings: list = Field(default_factory=list)  # rule names that caused it

    @classmethod
    def allow(cls) -> "GateDecision":
        return cls(decision="allow")


class Command(BaseModel):
    command_id: str = Field(default_factory=lambda: f"cmd-{next(_cmd_counter)}")
    type: Literal["inject_message", "interrupt", "request_replan"]
    agent_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class ControlAck(BaseModel):
    command_id: str
    status: Literal["accepted", "applied", "ignored", "unsupported", "failed"]
    step: int = 0
    detail: str = ""
