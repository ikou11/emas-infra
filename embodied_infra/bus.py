"""Synchronous in-process event bus.

Handlers run in subscription order on the caller's thread. A failing handler is
logged and skipped; it never breaks the publisher (the EMAS main loop).
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable, List, Optional, Set, Tuple

from .events import Event

logger = logging.getLogger("embodied_infra.bus")
Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: List[Tuple[Optional[Set[str]], Handler, str]] = []
        self.errors: List[str] = []

    def subscribe(self, handler: Handler, types: Optional[Iterable[str]] = None,
                  name: str = "") -> None:
        self._subs.append((set(types) if types else None, handler, name or repr(handler)))

    def publish(self, event: Event) -> None:
        for types, handler, name in self._subs:
            if types is not None and event.type not in types:
                continue
            try:
                handler(event)
            except Exception as e:  # isolate subscriber failures
                msg = f"handler {name} failed on {event.type}#{event.seq}: {e!r}"
                logger.warning(msg)
                self.errors.append(msg)
