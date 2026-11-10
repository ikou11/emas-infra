"""Infra configuration (docs/02-v1-interface-spec.md §8)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import yaml
from pydantic import BaseModel, Field


class TraceCfg(BaseModel):
    enabled: bool = True
    dir: Optional[str] = None  # None -> no files written


class WorldModelCfg(BaseModel):
    enabled: bool = True
    allowed_sources: Optional[List[str]] = None  # None -> accept all sources


class ToolsCfg(BaseModel):
    enabled: bool = True
    expose_in: List[str] = Field(default_factory=lambda: ["discussion"])


class SupervisorCfg(BaseModel):
    enabled: bool = True
    rules: List[str] = Field(default_factory=lambda: [
        "unknown_action", "unknown_entity", "capability", "precondition",
        "outcome_failure", "repeat_failure", "idle_deadlock"])
    rule_params: Dict[str, Dict[str, Any]] = Field(default_factory=lambda: {"repeat_failure": {"n": 2}})


class InterventionsCfg(BaseModel):
    allowed: List[str] = Field(default_factory=lambda: [
        "gate_deny", "gate_replace", "inject_message", "interrupt"])
    policy: Dict[str, Dict[str, Any]] = Field(default_factory=dict)  # overrides DEFAULT_POLICY
    cooldown_steps: int = 50
    max_per_agent_episode: int = 5
    gate_timeout_s: float = 2.0


class InfraConfig(BaseModel):
    enabled: bool = False
    trace: TraceCfg = Field(default_factory=TraceCfg)
    world_model: WorldModelCfg = Field(default_factory=WorldModelCfg)
    tools: ToolsCfg = Field(default_factory=ToolsCfg)
    supervisor: SupervisorCfg = Field(default_factory=SupervisorCfg)
    interventions: InterventionsCfg = Field(default_factory=InterventionsCfg)


def load_config(src: Union[None, str, Dict[str, Any], InfraConfig] = None) -> InfraConfig:
    """Accepts a YAML path, a dict (e.g. an OmegaConf container) or a config object.
    A top-level `infra:` key is unwrapped."""
    if src is None:
        return InfraConfig()
    if isinstance(src, InfraConfig):
        return src
    if isinstance(src, str):
        with open(src, "r", encoding="utf-8") as f:
            src = yaml.safe_load(f) or {}
    if "infra" in src and isinstance(src["infra"], dict):
        src = src["infra"]
    return InfraConfig.model_validate(src)
