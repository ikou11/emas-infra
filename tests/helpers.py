import os
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TRACES = os.path.join(HERE, "fixtures", "traces")
EMOS_PROFILE = os.path.join(ROOT, "examples", "emos", "profile.yaml")


def trace(name: str) -> str:
    return os.path.join(TRACES, name)


class EventFactory:
    """Builds minimal, contract-conforming events for synthetic scenarios."""

    def __init__(self, episode_id: str = "t1") -> None:
        self.seq = 0
        self.episode_id = episode_id
        self.step = 0

    def __call__(self, type_: str, data: Optional[Dict[str, Any]] = None,
                 agent_id: Optional[str] = None, source: str = "system",
                 step: Optional[int] = None) -> Dict[str, Any]:
        self.seq += 1
        if step is not None:
            self.step = step
        return {"schema": "embodied-infra/event/0.1", "seq": self.seq, "session_id": "s",
                "episode_id": self.episode_id, "step": self.step, "t_wall": 0.0,
                "type": type_, "agent_id": agent_id, "source": source, "data": data or {}}


def emos_action_space() -> List[Dict[str, Any]]:
    import yaml
    with open(EMOS_PROFILE) as f:
        return yaml.safe_load(f)["action_space"]


def start_episode(rt, f: EventFactory, agents: Dict[str, List[str]],
                  entities=("cup", "TARGET_cup"), goal=("at(cup,TARGET_cup)",),
                  supports=("gate", "inject_message", "interrupt")) -> None:
    """agents: {agent_id: capability_tags}"""
    rt.emit(f("episode.started", {
        "task_text": "put the cup on the target",
        "goal": {"conditions": list(goal)},
        "entities": [{"entity_id": e, "type": "obj"} for e in entities]}))
    for aid, tags in agents.items():
        rt.emit(f("agent.registered", {"embodiment": "Robot", "capabilities": {},
                                       "capability_tags": tags,
                                       "action_space": emos_action_space(),
                                       "supports": list(supports)}, agent_id=aid))


def snapshot(f: EventFactory, holding: Dict[str, Optional[str]], predicates=(), step=None):
    return f("state.snapshot", {"reason": "action_boundary",
                                "agents": {a: {"pos": [0, 0, 0], "yaw": 0, "holding": h}
                                           for a, h in holding.items()},
                                "entities": {}, "predicates": list(predicates)},
             source="sim_gt", step=step)
