"""The two sides of the southbound contract (docs/02-v1-interface-spec.md §7).

EMAS code depends only on `InfraClient`. With infra disabled it receives a
`NullInfraClient`, which guarantees unchanged EMAS behaviour.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Union

from .control import Command, ControlAck, GateDecision
from .events import Event

EventLike = Union[Event, Dict[str, Any]]


class EmasAdapter(ABC):
    """Implemented by the EMAS side. Infra calls it to deliver commands."""

    @abstractmethod
    def control(self, cmd: Command) -> ControlAck:
        """Must return immediately (queue the command), never block."""


class InfraClient(ABC):
    """Implemented by infra. The adapter reports events and asks the gate."""

    enabled: bool = True

    @abstractmethod
    def attach(self, adapter: EmasAdapter) -> None: ...

    @abstractmethod
    def emit(self, event: EventLike) -> None:
        """Non-blocking for the caller's purposes and never raises."""

    @abstractmethod
    def gate(self, event: EventLike, timeout_s: float = 2.0) -> GateDecision: ...

    @abstractmethod
    def tool_schemas(self, agent_id: str) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def call_tool(self, agent_id: str, name: str, args: Dict[str, Any]) -> str: ...


class NullInfraClient(InfraClient):
    enabled = False

    def attach(self, adapter: EmasAdapter) -> None:
        pass

    def emit(self, event: EventLike) -> None:
        pass

    def gate(self, event: EventLike, timeout_s: float = 2.0) -> GateDecision:
        return GateDecision.allow()

    def tool_schemas(self, agent_id: str) -> List[Dict[str, Any]]:
        return []

    def call_tool(self, agent_id: str, name: str, args: Dict[str, Any]) -> str:
        return '{"error": "infra disabled"}'
