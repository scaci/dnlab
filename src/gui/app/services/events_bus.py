"""In-process event bus for lab progress events.

The dnlab-multinode controllers emit progress events from their own
worker thread (via :class:`dnlab_multinode.ProgressEvent`). Those events
need to fan out to zero-or-more WebSocket subscribers without blocking
the controller. This module provides a simple pub/sub:

* one :class:`EventsBus` for application, held as a module-level
  singleton (see :data:`bus` at the bottom);
* one ring buffer for lab so a late-joining WebSocket client can
  replay recent history on connect;
* one ``asyncio.Queue`` for subscriber, fed from a thread-safe
  publish API that schedules the enqueue on the event loop via
  :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe`.

The bus is intentionally tiny — ~80 lines — and has no external
dependencies. It exists so :class:`~app.services.multinode_service.MultinodeService`
can hand the controller a synchronous callback that "just works" from
the controller thread, while WebSocket handlers consume events
asynchronously.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class BusEvent:
    """Serialisable event record pushed to WS subscribers."""
    lab: str
    phase: str
    status: str
    host: str | None = None
    detail: str = ""
    elapsed_ms: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class EventsBus:
    """Per-lab pub/sub with a replay ring buffer.

    Thread-safety: :meth:`publish` is safe to call from any thread
    (it schedules queue puts on the owning event loop). All other
    methods must be called from the event loop thread.
    """

    def __init__(self, buffer_size: int = 500):
        self._buffer_size = buffer_size
        self._history: dict[str, deque[BusEvent]] = defaultdict(
            lambda: deque(maxlen=buffer_size)
        )
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── lifecycle ─────────────────────────────────────────────────

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the owning event loop. Called once at app startup."""
        self._loop = loop

    # ── publish (thread-safe) ─────────────────────────────────────

    def publish(self, event: BusEvent) -> None:
        """Push an event to all current subscribers of ``event.lab``.

        Safe to call from a worker thread: the actual queue operations
        are scheduled on the event loop.
        """
        self._history[event.lab].append(event)
        if self._loop is None:
            return
        for q in list(self._subscribers.get(event.lab, ())):
            self._loop.call_soon_threadsafe(self._try_put, q, event)

    @staticmethod
    def _try_put(q: asyncio.Queue, event: BusEvent) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("events_bus: subscriber queue full — dropping event")

    # ── subscribe / unsubscribe (event-loop only) ─────────────────

    def subscribe(self, lab: str, *, replay: bool = True) -> asyncio.Queue:
        """Register a new subscriber. Returns an :class:`asyncio.Queue`
        that receives :class:`BusEvent` instances. If ``replay`` is
        true the queue is pre-filled with the ring-buffer history.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=self._buffer_size * 2)
        self._subscribers[lab].add(q)
        if replay:
            for evt in list(self._history.get(lab, ())):
                q.put_nowait(evt)
        return q

    def unsubscribe(self, lab: str, q: asyncio.Queue) -> None:
        self._subscribers.get(lab, set()).discard(q)

    # ── introspection ─────────────────────────────────────────────

    def history(self, lab: str) -> list[BusEvent]:
        return list(self._history.get(lab, ()))

    def subscriber_count(self, lab: str) -> int:
        return len(self._subscribers.get(lab, ()))


#: Module-level singleton used by MultinodeService + WS routes.
bus = EventsBus()
