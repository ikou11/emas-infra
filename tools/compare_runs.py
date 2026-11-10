#!/usr/bin/env python3
"""Compare infra-on vs infra-off runs from their traces (EMAS-agnostic).

Usage:
    python compare_runs.py <run_dir> [<run_dir> ...] [--json out.json]

A run dir contains trace/episode_*.jsonl (adapter event stream) and, when infra
was on, trace/episode_*.infra.jsonl. The arm is "on" if any *.infra.jsonl
exists, else "off". Per episode: success, steps, number of actions, LLM tokens
(per-agent running total from llm.usage, summed; execution phase only), findings and interventions by rule.
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load(p: Path):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def episode_stats(trace: Path):
    ev = load(trace)
    end = next((e for e in reversed(ev) if e["type"] == "episode.ended"), None)
    last_usage = {}  # EMOS reports a per-agent running total for the episode
    for e in ev:
        if e["type"] == "llm.usage":
            last_usage[e["agent_id"]] = int(e["data"].get("total_tokens") or 0)
    tokens = sum(last_usage.values())
    st = {
        "episode_id": ev[0]["episode_id"] if ev else None,
        "complete": end is not None,
        "success": bool(end and end["data"].get("success")),
        "steps": end["data"].get("steps") if end else None,
        "actions": sum(1 for e in ev if e["type"] == "action.started"),
        "rejected": sum(1 for e in ev if e["type"] == "action.ended"
                        and e["data"].get("status") == "rejected"),
        "tokens": tokens,
        "findings": Counter(), "interventions": Counter(),
    }
    infra = trace.with_name(trace.name[:-len(".jsonl")] + ".infra.jsonl")
    if infra.exists():
        for r in load(infra):
            if r["type"] == "infra.finding":
                st["findings"][r["data"]["rule"]] += 1
            elif r["type"] == "intervention.issued":
                st["interventions"][r["data"]["command"]["type"]] += 1
            elif r["type"] == "infra.gate_decision" and \
                    r["data"]["decision"]["decision"] == "deny":
                st["interventions"]["gate_deny"] += 1
    return st


def main(argv):
    out_json = None
    if "--json" in argv:
        i = argv.index("--json"); out_json = argv[i + 1]; argv = argv[:i] + argv[i + 2:]
    runs = []
    for a in argv:
        d = Path(a)
        traces = sorted(x for x in (d / "trace").glob("episode_*.jsonl")
                        if not x.name.endswith(".infra.jsonl"))
        if not traces:
            print(f"skip {d}: no trace"); continue
        arm = "on" if list((d / "trace").glob("*.infra.jsonl")) else "off"
        runs.append({"run": d.name, "arm": arm, "episodes": [episode_stats(t) for t in traces]})

    # per arm aggregate + per episode success matrix
    by_arm = defaultdict(list)
    per_ep = defaultdict(lambda: defaultdict(list))
    for r in runs:
        for e in r["episodes"]:
            if not e["complete"]:
                continue
            by_arm[r["arm"]].append(e)
            per_ep[e["episode_id"]][r["arm"]].append(e["success"])

    print(f"{'arm':4} {'n':>3} {'success':>9} {'steps(mean)':>12} {'actions':>8} {'tokens':>9}")
    for arm in ("off", "on"):
        es = by_arm.get(arm, [])
        if not es:
            continue
        n = len(es); s = sum(e["success"] for e in es)
        print(f"{arm:4} {n:3d} {s:3d}/{n:<3d}{100*s/n:4.0f}% "
              f"{sum(e['steps'] or 0 for e in es)/n:12.0f} "
              f"{sum(e['actions'] for e in es)/n:8.1f} {sum(e['tokens'] for e in es)/n:9.0f}")
    print("\nper episode (successes / runs):")
    for ep in sorted(per_ep, key=lambda x: (len(x), x)):
        cells = "  ".join(f"{arm}={sum(per_ep[ep][arm])}/{len(per_ep[ep][arm])}"
                          for arm in ("off", "on") if per_ep[ep][arm])
        print(f"  ep {ep:>5}: {cells}")
    f = sum((e["findings"] for e in by_arm.get("on", [])), Counter())
    iv = sum((e["interventions"] for e in by_arm.get("on", [])), Counter())
    if f or iv:
        print("\ninfra-on findings:", dict(f))
        print("infra-on interventions:", dict(iv))
    incomplete = [(r["run"], e["episode_id"]) for r in runs for e in r["episodes"]
                  if not e["complete"]]
    if incomplete:
        print("\nincomplete episodes (excluded):", incomplete)
    if out_json:
        for r in runs:
            for e in r["episodes"]:
                e["findings"] = dict(e["findings"]); e["interventions"] = dict(e["interventions"])
        Path(out_json).write_text(json.dumps(runs, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
