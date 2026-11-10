"""Action-semantics condition language (docs/02-v1-interface-spec.md §3.3.1).

    cond := term ("==" | "!=") term
          | "pred(" NAME {"," term} ")"
          | "not " cond
    term := "null" | "agent.id" | "agent.holding" | "args." NAME | "'" literal "'"

Evaluation is three-valued: True / False / None (= unknown, e.g. the needed
information was never observed). Rules only fire on a definite False.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Union

_MISSING = object()


class SemanticsError(ValueError):
    pass


@dataclass(frozen=True)
class Term:
    kind: str   # null | agent | args | lit
    value: str = ""


@dataclass(frozen=True)
class Compare:
    left: Term
    op: str
    right: Term


@dataclass(frozen=True)
class Pred:
    name: str
    args: tuple


@dataclass(frozen=True)
class Not:
    inner: "Cond"


Cond = Union[Compare, Pred, Not]

_TERM_RE = re.compile(r"^(null|agent\.(id|holding)|args\.[A-Za-z_][A-Za-z0-9_]*|'[^']*')$")
_PRED_RE = re.compile(r"^pred\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*((?:,\s*[^,()]+)*)\)$")


def _term(tok: str) -> Term:
    tok = tok.strip()
    if not _TERM_RE.match(tok):
        raise SemanticsError(f"bad term {tok!r}")
    if tok == "null":
        return Term("null")
    if tok.startswith("agent."):
        return Term("agent", tok[len("agent."):])
    if tok.startswith("args."):
        return Term("args", tok[len("args."):])
    return Term("lit", tok[1:-1])


def parse(text: str) -> Cond:
    t = text.strip()
    if t.startswith("not "):
        return Not(parse(t[4:]))
    m = _PRED_RE.match(t)
    if m:
        raw_args = [a for a in m.group(2).split(",") if a.strip()]
        return Pred(m.group(1), tuple(_term(a) for a in raw_args))
    for op in ("==", "!="):
        if op in t:
            left, right = t.split(op, 1)
            return Compare(_term(left), op, _term(right))
    raise SemanticsError(f"cannot parse condition {text!r}")


@dataclass
class EvalContext:
    agent_id: str
    agent_holding: Any = _MISSING          # _MISSING = unknown
    args: Optional[Dict[str, Any]] = None
    predicates: Optional[Set[str]] = None  # None = never observed


def _value(term: Term, ctx: EvalContext) -> Any:
    if term.kind == "null":
        return None
    if term.kind == "lit":
        return term.value
    if term.kind == "agent":
        return ctx.agent_id if term.value == "id" else ctx.agent_holding
    if term.kind == "args":
        if not isinstance(ctx.args, dict) or term.value not in ctx.args:
            return _MISSING
        return ctx.args[term.value]
    return _MISSING


def evaluate(cond: Cond, ctx: EvalContext) -> Optional[bool]:
    if isinstance(cond, Not):
        r = evaluate(cond.inner, ctx)
        return None if r is None else not r
    if isinstance(cond, Compare):
        lv, rv = _value(cond.left, ctx), _value(cond.right, ctx)
        if lv is _MISSING or rv is _MISSING:
            return None
        return (lv == rv) if cond.op == "==" else (lv != rv)
    if isinstance(cond, Pred):
        if ctx.predicates is None:
            return None
        vals = [_value(a, ctx) for a in cond.args]
        if any(v is _MISSING or v is None for v in vals):
            return None
        return f"{cond.name}({','.join(str(v) for v in vals)})" in ctx.predicates
    raise SemanticsError(f"unknown condition node {cond!r}")


@dataclass
class CheckResult:
    ok: Optional[bool]               # True all hold, False some failed, None unknown
    failed: List[str]
    unknown: List[str]


def check_all(conds: Sequence[str], ctx: EvalContext) -> CheckResult:
    failed, unknown = [], []
    for text in conds or []:
        r = evaluate(parse(text), ctx)
        if r is False:
            failed.append(text)
        elif r is None:
            unknown.append(text)
    if failed:
        return CheckResult(False, failed, unknown)
    if unknown:
        return CheckResult(None, failed, unknown)
    return CheckResult(True, failed, unknown)


def context_for(world, agent_id: str, args: Any) -> EvalContext:
    agent = world.agents.get(agent_id)
    holding = _MISSING
    if agent is not None and agent.holding_known:
        holding = agent.holding
    return EvalContext(agent_id=agent_id, agent_holding=holding,
                       args=args if isinstance(args, dict) else None,
                       predicates=world.predicates)


def validate_table(action_space: Dict[str, Any]) -> List[str]:
    """Return parse errors for every condition in an agent's action space."""
    errs = []
    for name, spec in action_space.items():
        sem = spec.semantics if hasattr(spec, "semantics") else spec.get("semantics", {})
        for key in ("pre", "post"):
            for c in sem.get(key, []) or []:
                try:
                    parse(c)
                except SemanticsError as e:
                    errs.append(f"{name}.{key}: {e}")
    return errs
