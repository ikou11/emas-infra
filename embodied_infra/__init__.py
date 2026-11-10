"""Embodied Infra: runtime infrastructure for embodied multi-agent systems (v1)."""
from .client import EmasAdapter, InfraClient, NullInfraClient
from .config import InfraConfig, load_config
from .control import Command, ControlAck, GateDecision
from .events import Event
from .runtime import InfraRuntime, make_client

__version__ = "0.1.0"
__all__ = ["EmasAdapter", "InfraClient", "NullInfraClient", "InfraConfig", "load_config",
           "Command", "ControlAck", "GateDecision", "Event", "InfraRuntime", "make_client"]
