"""LLM part of the supervisor.

Division of labour (docs/07-supervisor-llm.md):
  * rules decide WHEN something is wrong (cheap, deterministic, synchronous gate);
  * the LLM is called only for escalated findings (or once per episode for plan
    review) and decides WHAT to do: which agents to tell what, whether to interrupt.
  * if the LLM is unavailable, slow, out of budget or returns garbage, the rule
    templates are used instead (fallback) -- the LLM can only improve interventions.

The LLM sees the infra world state (v1: including simulator ground truth) but is
instructed to point out problems and suggest, not to hand the EMAS facts it would
otherwise have to discover itself.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..control import Command
from ..world.model import WorldState
from .findings import Finding
from .policy import MESSAGE_PREFIX

logger = logging.getLogger("embodied_infra.llm")

SYSTEM_PROMPT = """You are the supervisor of an embodied multi-agent system (several robots \
controlled by their own LLM agents) working on one shared task. You do NOT control the robots. \
You can only send short messages to an agent (they read it before their next decision) and \
interrupt an agent's current action (it then immediately decides again).

You are called only when an automatic rule detected a problem. Decide the most useful, minimal \
intervention:
- Diagnose from the action history and outcomes (e.g. an agent keeps failing on an object it \
cannot reach -> a capable idle teammate should take over; an agent waits while work remains).
- Address the agent(s) who can act on it. Messages must be concrete and short (<= 3 sentences), \
say what went wrong and what to do next. Name entity ids exactly as given.
- Interrupt an agent only if its current action is useless (e.g. waiting while it should work, \
or stuck); never interrupt an agent that is making progress.
- Do not reveal information the agents could not know (such as exact object positions); point \
out problems and suggest who should do what.
- Use `known_failures`: an agent that already failed an action on an object most likely cannot \
do it (e.g. cannot reach it). Prefer handing that work to a capable teammate over retrying, and \
never send two agents to the same object.
- Check physical feasibility yourself with `scene` (object heights, distances) and each agent's \
`capabilities` (e.g. arm workspace) before asking an agent to do something. The rules only know \
coarse capability tags: if a detected problem suggests that an agent takes over work it \
physically cannot do, call no_intervention instead.
- Respect what the agents said in `agent_messages`: if an agent explained that it cannot do \
something (e.g. out of reach), do not push it to do it unless you have evidence it is wrong. \
An uneven plan may be deliberate because of such limits.
- Stay consistent with your own `supervisor_notes` (e.g. your plan review) unless the \
situation has changed.
- If the rule's default intervention is already right, call use_default. If nothing should be \
done, call no_intervention.
Think briefly, then respond only with tool calls (you may call several)."""

PLAN_REVIEW_PROMPT = """You are the supervisor of an embodied multi-agent system. The agents just \
agreed on a plan (one subtask per agent). Review it against the goal, the robots' capabilities \
and the time budget. Typical problems: all work given to one robot while another idles although \
it is capable (work done serially may not fit the time budget), a subtask given to a robot that \
lacks the capability, goal conditions nobody is responsible for.
Before suggesting that an idle robot takes over work, VERIFY it is physically able to: compare \
object heights/distances in `scene` with the robot's `capabilities` (e.g. arm workspace bounds). \
An uneven plan is often deliberate because a robot cannot reach an object; then it is correct. \
If the plan is fine or you are unsure, call no_intervention. Otherwise send each affected agent \
one short, concrete suggestion. Do not interrupt anyone. Think briefly, then respond only with \
tool calls."""

TOOLS = [
    {"type": "function", "function": {
        "name": "inject_message",
        "description": "Send a short message to one agent; it reads it before its next decision.",
        "parameters": {"type": "object", "properties": {
            "agent_id": {"type": "string"},
            "text": {"type": "string", "description": "<= 3 sentences"}},
            "required": ["agent_id", "text"]}}},
    {"type": "function", "function": {
        "name": "interrupt",
        "description": "Stop the agent's current action so that it decides again now.",
        "parameters": {"type": "object", "properties": {
            "agent_id": {"type": "string"},
            "reason": {"type": "string"}},
            "required": ["agent_id", "reason"]}}},
    {"type": "function", "function": {
        "name": "use_default",
        "description": "Apply the rule-based default intervention unchanged.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "no_intervention",
        "description": "Do nothing now.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"}}, "required": ["reason"]}}},
]

# A chat function: (messages, tools) -> list of {"name": str, "arguments": dict}
ChatFn = Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], List[Dict[str, Any]]]


# --------------------------------------------------------------------- client
class NoToolCall(RuntimeError):
    """The model answered without a tool call (e.g. ran out of tokens while reasoning)."""


def _read_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def resolve_env(env_file: Optional[str]) -> Dict[str, str]:
    """Supervisor LLM settings: explicit file > $EMBODIED_INFRA_ENV > <repo>/.env,
    then process environment variables for anything missing."""
    candidates = [env_file, os.getenv("EMBODIED_INFRA_ENV"),
                  str(Path(__file__).resolve().parents[2] / ".env")]
    env: Dict[str, str] = {}
    for c in candidates:
        if c and Path(c).is_file():
            env = _read_env_file(Path(c))
            break
    for k in ("SUPERVISOR_LLM_API_KEY", "SUPERVISOR_LLM_BASE_URL", "SUPERVISOR_LLM_MODEL"):
        if k not in env and os.getenv(k):
            env[k] = os.environ[k]
    return env


def openai_chat_fn(env_file: Optional[str], model: Optional[str], timeout_s: float,
                   temperature: float, max_tokens: int,
                   tool_choice: str = "auto") -> Optional[ChatFn]:
    """Build a chat function for an OpenAI-compatible endpoint; None if unavailable."""
    env = resolve_env(env_file)
    key = env.get("SUPERVISOR_LLM_API_KEY")
    model = model or env.get("SUPERVISOR_LLM_MODEL")
    if not key or not model:
        logger.warning("supervisor LLM: no credentials/model configured; using rule fallback")
        return None
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("supervisor LLM: `openai` package missing; using rule fallback")
        return None
    client = OpenAI(api_key=key, base_url=env.get("SUPERVISOR_LLM_BASE_URL") or None,
                    timeout=timeout_s, max_retries=1)

    def chat(messages, tools):
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice=tool_choice,
            temperature=temperature, max_tokens=max_tokens)
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"name": tc.function.name, "arguments": args})
        if not calls:
            raise NoToolCall(f"finish_reason={resp.choices[0].finish_reason}, "
                             f"content={(msg.content or '')[:200]!r}")
        usage = getattr(resp, "usage", None)
        if usage is not None:
            calls[0]["_usage"] = {"prompt": usage.prompt_tokens,
                                  "completion": usage.completion_tokens}
        return calls

    chat.model = model  # type: ignore[attr-defined]
    return chat


# -------------------------------------------------------------------- context
def _compact_caps(caps: Dict[str, Any]) -> Dict[str, Any]:
    """Keep machine-readable capability fields (e.g. workspaces), drop long prose."""
    out: Dict[str, Any] = {}
    for k, v in (caps or {}).items():
        if isinstance(v, dict):
            sub = {kk: vv for kk, vv in v.items() if kk != "summary"}
            if sub:
                out[k] = sub
        elif k not in ("robot_id",):
            out[k] = v
    return out


def _agent_view(world: WorldState, agent_id: str, n_actions: int = 6) -> Dict[str, Any]:
    a = world.agents[agent_id]
    cur = world.actions.get(a.current_action) if a.current_action else None
    hist = []
    for r in world.agent_actions(agent_id)[-n_actions:]:
        args = {k: v for k, v in r.args.items() if k != "robot"} if isinstance(r.args, dict) else r.args
        hist.append({"action": r.name, "args": args, "steps": [r.started_step, r.ended_step],
                     "status": r.status, "outcome": r.outcome})
    return {
        "embodiment": a.embodiment, "capability_tags": a.capability_tags,
        "capabilities": _compact_caps(a.capabilities),
        "actions_available": sorted(a.action_space),
        "holding": a.holding if a.holding_known else "unknown",
        "subtask": a.subtask,
        "current_action": None if cur is None else {
            "action": cur.name, "running_steps": world.step - (cur.started_step or world.step)},
        "recent_actions": hist,
    }


def _known_failures(world: WorldState) -> List[Dict[str, Any]]:
    out = []
    for aid in world.action_order:
        r = world.actions[aid]
        if r.outcome == "failure" or r.status in ("timeout", "error"):
            args = {k: v for k, v in r.args.items() if k != "robot"} if isinstance(r.args, dict) else r.args
            out.append({"agent": r.agent_id, "action": r.name, "args": args, "step": r.ended_step,
                        "status": r.status, "outcome": r.outcome})
    return out[-10:]


def build_context(world: WorldState, findings: List[Finding], default: List[Command],
                  recent_interventions: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "step": world.step,
        "max_steps": world.max_steps,
        "task": (world.task_text or "")[:1500],
        "scene": (world.scene_text or "")[:2000],
        "agent_messages": [{"step": m.get("step"), "from": m.get("from"), "to": m.get("to"),
                            "text": str(m.get("text", ""))[:400]} for m in world.messages[-6:]],
        "goal_conditions": {c: (c in (world.predicates or set()))
                            for c in (world.goal_conditions or [])},
        "agents": {aid: _agent_view(world, aid) for aid in world.agents},
        "known_failures": _known_failures(world),
        "detected_problems": [{"rule": f.rule, "agent": f.agent_id, "message": f.message,
                               "evidence": f.evidence} for f in findings],
        "default_intervention": [{"type": c.type, "agent_id": c.agent_id,
                                  "text": c.payload.get("text")} for c in default],
        # the supervisor's own earlier decisions (incl. reasons for not intervening), so that
        # later calls stay consistent with e.g. the plan review
        "supervisor_notes": recent_interventions[-6:],
    }


# ----------------------------------------------------------------- supervisor
class LLMSupervisor:
    def __init__(self, chat: Optional[ChatFn], max_calls_per_episode: int = 8,
                 min_interval_steps: int = 30, allowed: Optional[List[str]] = None) -> None:
        self.chat = chat
        self.max_calls = max_calls_per_episode
        self.min_interval = min_interval_steps
        self.allowed = set(allowed) if allowed is not None else {"inject_message", "interrupt"}
        self.reset()

    @property
    def available(self) -> bool:
        return self.chat is not None

    def reset(self) -> None:
        self.calls = 0
        self.last_step: Optional[int] = None
        self.history: List[Dict[str, Any]] = []

    def _can_call(self, step: int, ignore_interval: bool = False) -> Tuple[bool, str]:
        if self.chat is None:
            return False, "llm unavailable"
        if self.calls >= self.max_calls:
            return False, "llm budget exhausted"
        if not ignore_interval and self.last_step is not None and \
                step - self.last_step < self.min_interval:
            return False, f"llm min interval ({step - self.last_step} < {self.min_interval})"
        return True, ""

    def _to_commands(self, calls: List[Dict[str, Any]], world: WorldState,
                     default: List[Command]) -> Optional[List[Command]]:
        cmds: List[Command] = []
        for c in calls:
            name, args = c.get("name"), c.get("arguments") or {}
            if name == "use_default":
                return list(default)
            if name == "no_intervention":
                continue
            aid = args.get("agent_id")
            if aid not in world.agents:
                continue
            if name == "inject_message" and "inject_message" in self.allowed:
                text = str(args.get("text", "")).strip()
                while text.startswith(MESSAGE_PREFIX):
                    text = text[len(MESSAGE_PREFIX):].strip()
                if text:
                    cmds.append(Command(type="inject_message", agent_id=aid,
                                        payload={"text": f"{MESSAGE_PREFIX} {text}",
                                                 "from": "supervisor_llm"}))
            elif name == "interrupt" and "interrupt" in self.allowed:
                cmds.append(Command(type="interrupt", agent_id=aid,
                                    payload={"reason": str(args.get("reason", "llm"))}))
        if not cmds and not any(c.get("name") == "no_intervention" for c in calls):
            return None  # nothing usable -> fallback
        # interrupts first, then messages (the agent reads them when it re-decides)
        cmds.sort(key=lambda c: c.type != "interrupt")
        return cmds

    def decide(self, world: WorldState, findings: List[Finding], default: List[Command],
               mode: str = "escalation") -> Tuple[Optional[List[Command]], Dict[str, Any]]:
        """Returns (commands or None for fallback, record for the infra trace)."""
        rec: Dict[str, Any] = {"mode": mode, "rules": sorted({f.rule for f in findings})}
        ok, why = self._can_call(world.step, ignore_interval=(mode == "plan_review"))
        if not ok:
            rec.update(used="fallback", reason=why)
            return None, rec
        ctx = build_context(world, findings, default, self.history)
        messages = [{"role": "system",
                     "content": PLAN_REVIEW_PROMPT if mode == "plan_review" else SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(ctx, ensure_ascii=False, default=str)}]
        self.calls += 1
        if mode != "plan_review":  # one-off per episode; must not block the first escalation
            self.last_step = world.step
        t0 = time.time()
        try:
            calls = self.chat(messages, TOOLS) or []
        except Exception as err:  # network, timeout, bad response
            rec.update(used="fallback", reason=f"llm error: {err!r}"[:300],
                       latency_s=round(time.time() - t0, 2))
            return None, rec
        rec["latency_s"] = round(time.time() - t0, 2)
        usage = calls[0].pop("_usage", None) if calls else None
        if usage:
            rec["usage"] = usage
        rec["tool_calls"] = calls
        cmds = self._to_commands(calls, world, default)
        if cmds is None:
            rec.update(used="fallback", reason="no usable tool call")
            return None, rec
        rec["used"] = "llm"
        for c in calls:
            a = c.get("arguments") or {}
            note = {"step": world.step, "mode": mode, "decision": c.get("name")}
            if a.get("agent_id"):
                note["agent_id"] = a["agent_id"]
            if a.get("text") or a.get("reason"):
                note["text"] = str(a.get("text") or a.get("reason"))[:400]
            self.history.append(note)
        return cmds, rec
