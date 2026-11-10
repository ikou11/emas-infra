"""JSONL trace writer.

Two files per episode, so the adapter's input stream stays replayable:
  episode_<id>.jsonl        events received from the adapter (verbatim)
  episode_<id>.infra.jsonl  records produced by infra (findings, gate decisions,
                            interventions, inferred outcomes, tool calls)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional, TextIO

from .events import Event


class TraceWriter:
    def __init__(self, out_dir: Optional[str]) -> None:
        self.out_dir = out_dir
        self._in: Optional[TextIO] = None
        self._infra: Optional[TextIO] = None
        self._infra_seq = 0
        self.episode_id: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.out_dir)

    def _open(self, episode_id: str) -> None:
        self.close()
        self.episode_id = episode_id
        if not self.enabled:
            return
        os.makedirs(self.out_dir, exist_ok=True)
        base = os.path.join(self.out_dir, f"episode_{episode_id}")
        self._in = open(base + ".jsonl", "w", encoding="utf-8")
        self._infra = open(base + ".infra.jsonl", "w", encoding="utf-8")

    def write_input(self, event: Event) -> None:
        if event.type == "episode.started" or event.episode_id != self.episode_id:
            self._open(str(event.episode_id))
        if self._in:
            self._in.write(json.dumps(event.to_json_dict(), ensure_ascii=False) + "\n")
            self._in.flush()

    def write_infra(self, type_: str, step: int, data: Dict[str, Any],
                    agent_id: Optional[str] = None) -> Dict[str, Any]:
        self._infra_seq += 1
        rec = {"seq": self._infra_seq, "episode_id": self.episode_id, "step": step,
               "t_wall": round(time.time(), 3), "type": type_, "agent_id": agent_id,
               "source": "infra", "data": data}
        if self._infra:
            self._infra.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self._infra.flush()
        return rec

    def close(self) -> None:
        for fh in (self._in, self._infra):
            if fh:
                fh.close()
        self._in = self._infra = None
