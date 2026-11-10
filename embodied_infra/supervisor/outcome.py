"""Infer action outcomes from the semantics table's `post` conditions.

An ended action is evaluated on the first state snapshot that arrives after its
`action.ended` event (the snapshot that reflects the action's effect).
"""
from __future__ import annotations

from typing import List, Tuple

from ..semantics import check_all, context_for
from ..world.model import ActionRecord, WorldState


class OutcomeInferer:
    def __init__(self) -> None:
        self._pending: List[str] = []

    def reset(self) -> None:
        self._pending = []

    def on_action_ended(self, action_id: str) -> None:
        self._pending.append(action_id)

    def on_snapshot(self, world: WorldState) -> List[Tuple[ActionRecord, str, str]]:
        """Returns [(record, outcome, detail)] for every pending action."""
        results = []
        for action_id in self._pending:
            rec = world.actions.get(action_id)
            if rec is None:
                continue
            if rec.outcome in ("success", "failure"):  # adapter already told us
                continue
            if rec.status == "rejected":
                results.append((rec, "n/a", "action was rejected before execution"))
                continue
            agent = world.agents.get(rec.agent_id)
            spec = agent.action_space.get(rec.name) if agent else None
            post = (spec.semantics.get("post") if spec else None) or []
            if not post:
                results.append((rec, "n/a", "no post conditions declared"))
                continue
            res = check_all(post, context_for(world, rec.agent_id, rec.args))
            if res.ok is True:
                results.append((rec, "success", "all post conditions hold"))
            elif res.ok is False:
                results.append((rec, "failure", "post conditions failed: " + "; ".join(res.failed)))
            else:
                results.append((rec, "unknown", "cannot evaluate: " + "; ".join(res.unknown)))
        self._pending = []
        return results
