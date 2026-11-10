#!/usr/bin/env python3
"""Validate an embodied-infra v1 trace (episode_*.jsonl) against the event contract.

Usage:
    python validate_trace.py <trace.jsonl | trace_dir> [...]

Checks (docs/02-v1-interface-spec.md):
  - envelope fields present, schema version, seq strictly increasing, step non-decreasing
  - episode.started first, episode.ended last, agent.registered present
  - per action_id: proposed -> started -> ended (or proposed -> ended(rejected))
  - at most one in-flight action per agent
  - a state.snapshot follows every action.ended (unless the episode ended)
Prints a short summary per file and exits non-zero if any error was found.
"""
import json
import sys
from collections import Counter
from pathlib import Path

ENVELOPE = ["schema", "seq", "session_id", "episode_id", "step", "t_wall",
            "type", "agent_id", "source", "data"]
REQUIRED_TYPES = ["episode.started", "agent.registered", "action.proposed",
                  "action.started", "action.ended", "episode.ended"]
STATUSES = {"completed", "timeout", "interrupted", "rejected", "error"}


def validate(path: Path):
    errors, warnings = [], []
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not events:
        return ["empty trace"], [], {}
    types = Counter(e.get("type") for e in events)

    last_seq, last_step = None, -1
    for i, e in enumerate(events):
        missing = [k for k in ENVELOPE if k not in e]
        if missing:
            errors.append(f"#{i} missing envelope fields {missing}")
            continue
        if not str(e["schema"]).startswith("embodied-infra/event/0."):
            errors.append(f"#{i} unexpected schema {e['schema']}")
        if last_seq is not None and e["seq"] <= last_seq:
            errors.append(f"#{i} seq not increasing ({last_seq} -> {e['seq']})")
        if e["step"] < last_step:
            errors.append(f"#{i} step decreased ({last_step} -> {e['step']})")
        last_seq, last_step = e["seq"], e["step"]

    if events[0]["type"] != "episode.started":
        errors.append("first event is not episode.started")
    if events[-1]["type"] != "episode.ended":
        errors.append("last event is not episode.ended (episode may have crashed)")
    for t in REQUIRED_TYPES:
        if types[t] == 0:
            errors.append(f"required event type missing: {t}")

    entity_ids = set()
    for e in events:
        if e["type"] == "episode.started":
            entity_ids = {x["entity_id"] for x in e["data"].get("entities", [])}

    state = {}       # action_id -> stage
    inflight = {}    # agent_id -> action_id
    pending_snapshot = False
    for i, e in enumerate(events):
        t, d, agent = e["type"], e["data"], e["agent_id"]
        if t == "state.snapshot":
            pending_snapshot = False
        if t == "action.proposed":
            aid = d["action_id"]
            if aid in state:
                errors.append(f"#{i} duplicate action_id {aid}")
            if agent in inflight:
                errors.append(f"#{i} {agent} proposes {aid} while {inflight[agent]} in flight")
            state[aid] = "proposed"
            inflight[agent] = aid
            args = d.get("args") or {}
            if isinstance(args, dict):
                for k in ("target_obj", "target_location"):
                    if k in args and entity_ids and args[k] not in entity_ids:
                        warnings.append(f"#{i} {aid} references unknown entity {args[k]!r}")
        elif t == "action.started":
            aid = d["action_id"]
            if state.get(aid) != "proposed":
                errors.append(f"#{i} started {aid} without proposed")
            state[aid] = "started"
        elif t == "action.ended":
            aid = d["action_id"]
            if d.get("status") not in STATUSES:
                errors.append(f"#{i} bad status {d.get('status')}")
            ok_prev = "started" if d.get("status") != "rejected" else "proposed"
            if state.get(aid) != ok_prev:
                errors.append(f"#{i} ended {aid} from stage {state.get(aid)}")
            state[aid] = "ended"
            inflight.pop(agent, None)
            pending_snapshot = True
        elif t == "episode.ended":
            pending_snapshot = False
    if pending_snapshot:
        warnings.append("last action.ended not followed by a snapshot")
    dangling = [a for a, s in state.items() if s != "ended"]
    if dangling:
        errors.append(f"actions never ended: {dangling}")

    ended = events[-1]["data"] if events[-1]["type"] == "episode.ended" else {}
    summary = {
        "events": len(events),
        "types": dict(types),
        "actions": sum(1 for s in state.values()),
        "success": ended.get("success"),
        "steps": ended.get("steps"),
    }
    return errors, warnings, summary


def main(argv):
    paths = []
    for a in argv:
        p = Path(a)
        if p.is_dir():
            # infra output records (*.infra.jsonl) are not adapter event streams
            paths += sorted(x for x in p.glob("episode_*.jsonl") if not x.name.endswith(".infra.jsonl"))
        else:
            paths.append(p)
    if not paths:
        print(__doc__)
        return 2
    bad = 0
    for p in paths:
        errors, warnings, summary = validate(p)
        status = "OK " if not errors else "BAD"
        print(f"[{status}] {p}  {json.dumps(summary, ensure_ascii=False)}")
        for w in warnings:
            print(f"   warn: {w}")
        for err in errors:
            print(f"   error: {err}")
        bad += bool(errors)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
