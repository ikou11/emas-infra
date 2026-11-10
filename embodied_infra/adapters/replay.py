"""Replay mock adapter: feed a recorded EMAS trace into infra (open loop).

The recorded trajectory is replayed as-is: gate decisions and commands are
computed and logged but cannot change what the recorded agents did. This
answers "where would the supervisor have intervened, and why?".

Usage:
    python -m embodied_infra.adapters.replay <trace.jsonl> [--profile profile.yaml]
           [--out DIR] [--quiet]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional

import yaml

from ..client import EmasAdapter
from ..control import Command, ControlAck
from ..runtime import InfraRuntime


def load_profile(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def split_and_goal(expr: str) -> Optional[List[str]]:
    """'and(a(x,y),b(z))' -> ['a(x,y)', 'b(z)'] (top-level split only)."""
    m = re.match(r"^\s*and\((.*)\)\s*$", expr or "")
    if not m:
        return None
    out, depth, cur = [], 0, ""
    for ch in m.group(1):
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
            continue
        depth += ch == "("
        depth -= ch == ")"
        cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def enrich(event: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    """Fill fields that older traces lack, using the adapter profile."""
    if not profile:
        return event
    t, d = event.get("type"), event.get("data", {})
    if t == "episode.started":
        goal = d.setdefault("goal", {}) or {}
        if not goal.get("conditions") and goal.get("expr"):
            goal["conditions"] = split_and_goal(goal["expr"])
        if d.get("max_steps") is None and profile.get("max_steps"):
            d["max_steps"] = int(profile["max_steps"])
    elif t == "agent.registered":
        if not any(a.get("semantics") for a in d.get("action_space", [])) and \
                profile.get("action_space"):
            d["action_space"] = profile["action_space"]
        if d.get("capability_tags") is None:
            tags = (profile.get("embodiment_tags") or {}).get(d.get("embodiment"))
            if tags is not None:
                d["capability_tags"] = tags
    return event


class RecordingAdapter(EmasAdapter):
    """Accepts every command and remembers it (open-loop replay)."""

    def __init__(self) -> None:
        self.commands: List[Command] = []

    def control(self, cmd: Command) -> ControlAck:
        self.commands.append(cmd)
        return ControlAck(command_id=cmd.command_id, status="accepted",
                          detail="replay: recorded only, trajectory is fixed")


def replay(trace_path: str, profile: Optional[Dict[str, Any]] = None,
           config: Any = None, supports_all: bool = True) -> InfraRuntime:
    rt = InfraRuntime(config if config is not None else {"enabled": True})
    adapter = RecordingAdapter()
    rt.attach(adapter)
    with open(trace_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            e = enrich(json.loads(line), profile or {})
            if supports_all and e.get("type") == "agent.registered":
                e["data"]["supports"] = ["gate", "inject_message", "interrupt"]
            if e.get("type") == "action.proposed":
                rt.emit(e)
                rt.gate(e)  # decision is logged in the infra trace
            else:
                rt.emit(e)
    rt.close()
    rt.adapter_log = adapter  # type: ignore[attr-defined]
    return rt


def format_timeline(rt: InfraRuntime) -> str:
    lines = []
    for r in rt.records:
        t, d = r["type"], r["data"]
        if t == "infra.finding":
            lines.append(f"step {r['step']:>4}  FINDING   {d['rule']:<16} {d.get('agent_id') or '':<8} {d['message']}")
        elif t == "infra.gate_decision":
            dec = d["decision"]
            lines.append(f"step {r['step']:>4}  GATE      {dec['decision']:<16} {r.get('agent_id') or '':<8} {d['action_id']} {dec.get('reason','')}")
        elif t == "intervention.issued":
            c = d["command"]
            lines.append(f"step {r['step']:>4}  COMMAND   {c['type']:<16} {c.get('agent_id') or '':<8} {c['payload'].get('text', c['payload'].get('reason', ''))}")
        elif t == "action.outcome_inferred" and d["outcome"] in ("success", "failure"):
            lines.append(f"step {r['step']:>4}  OUTCOME   {d['outcome']:<16} {r.get('agent_id') or '':<8} {d['action_id']} {d['name']} ({d['status']})")
        elif t == "supervisor.llm_decision":
            calls = "; ".join(f"{c['name']}({json.dumps(c.get('arguments', {}), ensure_ascii=False)})"
                              for c in d.get("tool_calls", []))
            lines.append(f"step {r['step']:>4}  LLM       {d['mode']:<16} {d.get('used'):<8} "
                         f"{d.get('reason') or calls} [{d.get('latency_s', '-')}s]")
        elif t == "infra.warning":
            lines.append(f"step {r['step']:>4}  WARNING   {json.dumps(d, ensure_ascii=False)}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trace")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--out", default=None, help="write infra trace files to this directory")
    ap.add_argument("--config", default=None, help="infra YAML config (e.g. the EMOS one)")
    ap.add_argument("--llm", action="store_true",
                    help="enable the supervisor LLM and all assist options (real API calls)")
    args = ap.parse_args(argv)
    from ..config import load_config
    cfg = load_config(args.config).model_dump() if args.config else {}
    cfg.update({"enabled": True, "trace": {"enabled": bool(args.out), "dir": args.out}})
    if args.llm:
        sup = cfg.setdefault("supervisor", {})
        sup.setdefault("llm", {})["enabled"] = True
        sup["assist"] = {"first_failure_help": True, "workload_balance": True, "plan_review": True}
    rt = replay(args.trace, load_profile(args.profile), cfg)
    w = rt.world
    print(format_timeline(rt))
    print(f"\nepisode {w.episode_id}: success={w.success} steps={w.step} "
          f"actions={len(w.actions)} findings={len(rt.findings)} "
          f"commands={len(rt.adapter_log.commands)} bus_errors={len(rt.bus.errors)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
