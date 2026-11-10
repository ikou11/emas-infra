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


# Safety checks that prevent the EMAS from executing actions that would crash it.
# They run whenever infra is enabled, independent of `supervisor.enabled`, so an
# on/off comparison of the supervisor is not confounded by crashes.
SAFETY_RULES = ["unknown_action", "unknown_entity"]

CORRECTION_RULES = ["capability", "precondition", "outcome_failure", "repeat_failure",
                    "idle_deadlock", "stalled_action", "repeated_denial", "stagnation",
                    "help_routing"]


class AssistCfg(BaseModel):
    """Optional capability supplements for the EMAS (off by default).

    They go beyond pointing out errors: they suggest *who should do what*.
    """
    first_failure_help: bool = False   # help_routing also on the first failure
    workload_balance: bool = False     # deadline-risk rule: suggest idle teammates take work
    plan_review: bool = False          # LLM reviews the EMAS plan once per episode


class LLMCfg(BaseModel):
    """LLM part of the supervisor. Rules decide *when*, the LLM decides *what*.

    Credentials come from an env file (SUPERVISOR_LLM_API_KEY / _BASE_URL / _MODEL),
    independent of the EMAS's own LLM. Missing credentials -> rule fallback only.
    """
    enabled: bool = False              # library default off; EMOS config turns it on
    env_file: Optional[str] = None     # default: $EMBODIED_INFRA_ENV, else <repo>/.env
    model: Optional[str] = None        # overrides SUPERVISOR_LLM_MODEL
    # Findings that are escalated to the LLM (others are handled by rule templates).
    escalate: List[str] = Field(default_factory=lambda: [
        "repeat_failure", "stalled_action", "idle_deadlock", "repeated_denial",
        "stagnation", "workload_risk"])
    # Findings whose rule-template fallback is too risky without the LLM's feasibility
    # check (rules only know coarse capability tags): dropped if the LLM cannot decide.
    # workload_risk suggestions backed by a recorded failure of the owner are exempt.
    require_confirmation: List[str] = Field(default_factory=lambda: ["workload_risk"])
    max_calls_per_episode: int = 8
    min_interval_steps: int = 30
    timeout_s: float = 90.0
    temperature: float = 0.2
    max_tokens: int = 8000           # reasoning models spend part of it thinking
    # "auto" works with reasoning models (e.g. deepseek-flash thinking mode rejects
    # "required"); a reply without tool calls falls back to the rule templates.
    tool_choice: str = "auto"


class SupervisorCfg(BaseModel):
    enabled: bool = True
    rules: List[str] = Field(default_factory=lambda: list(CORRECTION_RULES))
    rule_params: Dict[str, Dict[str, Any]] = Field(default_factory=lambda: {"repeat_failure": {"n": 2}})
    assist: AssistCfg = Field(default_factory=AssistCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)


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
