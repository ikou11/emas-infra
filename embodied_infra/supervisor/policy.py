"""Map findings to interventions, with anti-oscillation limits.

Policy is configuration, not code: which rule leads to which intervention,
cooldown per (rule, agent), and an intervention budget per agent per episode.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from ..control import Command, GateDecision
from .findings import Finding

DEFAULT_POLICY: Dict[str, Dict[str, object]] = {
    # always=True: never skipped by cooldown/budget (executing it would crash the EMAS)
    "unknown_action":  {"gate": "deny", "inject": True, "always": True},
    "unknown_entity":  {"gate": "deny", "inject": True, "always": True},
    "capability":      {"gate": "deny", "inject": True},
    "precondition":    {"gate": "deny", "inject": True},
    "outcome_failure": {"inject": True},
    "repeat_failure":  {"inject": True},
    "idle_deadlock":   {"interrupt": True, "inject": True},
    "stalled_action":  {"interrupt": True, "inject": True},
    # helpers are idle (e.g. in a long wait): interrupt so they read the hint now
    "help_routing":    {"interrupt": True, "inject": True},
    "repeated_denial": {"inject": True},
    "stagnation":      {"inject": True},
    "workload_risk":   {"interrupt": True, "inject": True},
}

MESSAGE_PREFIX = "[Supervisor]"


class InterventionPolicy:
    def __init__(self, rules: Optional[Dict[str, Dict[str, object]]] = None,
                 allowed: Optional[List[str]] = None, cooldown_steps: int = 50,
                 max_per_agent_episode: int = 5) -> None:
        self.rules = {**DEFAULT_POLICY, **(rules or {})}
        self.allowed = set(allowed) if allowed is not None else {
            "gate_deny", "gate_replace", "inject_message", "interrupt"}
        self.cooldown_steps = cooldown_steps
        self.max_per_agent_episode = max_per_agent_episode
        self.reset()

    def reset(self) -> None:
        self._last: Dict[Tuple[str, Optional[str]], int] = {}
        self._count: Dict[Optional[str], int] = defaultdict(int)

    # ---------------------------------------------------------------- limits
    def _admit(self, f: Finding, use_cooldown: bool = True) -> Tuple[bool, str]:
        if self.rules.get(f.rule, {}).get("always"):
            return True, ""
        key = (f.rule, f.agent_id)
        last = self._last.get(key)
        if use_cooldown and last is not None and f.step - last < self.cooldown_steps:
            return False, f"cooldown ({f.step - last} < {self.cooldown_steps} steps)"
        if self._count[f.agent_id] >= self.max_per_agent_episode:
            return False, "intervention budget exhausted"
        return True, ""

    def _commit(self, f: Finding) -> None:
        self._last[(f.rule, f.agent_id)] = f.step
        self._count[f.agent_id] += 1

    @staticmethod
    def _text(fs: List[Finding]) -> str:
        return MESSAGE_PREFIX + " " + " ".join(f.message for f in fs)

    # ------------------------------------------------------------- decisions
    def decide_gate(self, findings: List[Finding]) -> Tuple[GateDecision, List[dict]]:
        """For gate-phase findings of ONE proposed action."""
        log: List[dict] = []
        deny = []
        for f in findings:
            cfg = self.rules.get(f.rule, {})
            if cfg.get("gate") != "deny" or "gate_deny" not in self.allowed:
                log.append({"rule": f.rule, "action": "none", "reason": "policy"})
                continue
            # Gate denials are not rate-limited by cooldown (letting a known-bad action
            # through is worse than repeating a hint); the per-agent budget still applies.
            ok, why = self._admit(f, use_cooldown=False)
            if not ok:
                log.append({"rule": f.rule, "action": "none", "reason": why})
                continue
            deny.append(f)
        if not deny:
            return GateDecision.allow(), log
        for f in deny:
            self._commit(f)
            log.append({"rule": f.rule, "action": "deny"})
        inject = any(self.rules.get(f.rule, {}).get("inject") for f in deny) \
            and "inject_message" in self.allowed
        return GateDecision(decision="deny", reason="; ".join(f.rule for f in deny),
                            message=self._text(deny) if inject else None,
                            findings=[f.rule for f in deny]), log

    def admit_async(self, findings: List[Finding]) -> Tuple[List[Finding], List[dict]]:
        """Apply cooldown / budget; admitted findings are committed (counted)."""
        ok_fs: List[Finding] = []
        log: List[dict] = []
        for f in findings:
            ok, why = self._admit(f)
            if not ok:
                log.append({"rule": f.rule, "agent_id": f.agent_id, "action": "none", "reason": why})
                continue
            if not self.commands_for([f]):
                log.append({"rule": f.rule, "agent_id": f.agent_id, "action": "none",
                            "reason": "policy"})
                continue
            self._commit(f)
            ok_fs.append(f)
        return ok_fs, log

    def commands_for(self, findings: List[Finding]) -> List[Command]:
        """Rule-template commands for already admitted findings (the LLM fallback)."""
        cmds: List[Command] = []
        for f in findings:
            cfg = self.rules.get(f.rule, {})
            do_int = cfg.get("interrupt") if f.interrupt is None else f.interrupt
            if do_int and "interrupt" in self.allowed:
                cmds.append(Command(type="interrupt", agent_id=f.agent_id,
                                    payload={"reason": f.rule}))
            if cfg.get("inject") and "inject_message" in self.allowed:
                cmds.append(Command(type="inject_message", agent_id=f.agent_id,
                                    payload={"text": self._text([f]), "from": "supervisor"}))
        return cmds

    def decide_async(self, findings: List[Finding]) -> Tuple[List[Command], List[dict]]:
        admitted, log = self.admit_async(findings)
        cmds = self.commands_for(admitted)
        for f in admitted:
            log.append({"rule": f.rule, "agent_id": f.agent_id,
                        "action": [c.type for c in self.commands_for([f])]})
        return cmds, log
