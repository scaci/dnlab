"""Progress event model + callback helpers.

The long-running controllers (``DeployController``, ``DestroyController``,
``SyncController``, ``StatusController``) emit :class:`ProgressEvent`
instances through an optional ``progress`` callback so that external
consumers (GUI, tests, alternative CLIs) can observe phase transitions
in real time without parsing log output.

The callback signature is simply ``Callable[[ProgressEvent], None]``.
Callbacks must be cheap and non-blocking — controllers call them on
their own (synchronous) thread, so expensive work should be offloaded.

When no callback is supplied, controllers use :func:`NullProgress`,
which is a no-op.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable


log = logging.getLogger(__name__)


@dataclass
class ProgressEvent:
    """A single observable step of a long-running operation.

    Attributes
    ----------
    phase:
        Machine-readable phase identifier, e.g. ``"mgmt-setup"``,
        ``"dnlab-deploy"``, ``"vxlan"``, ``"runtime-relay"``, ``"dns"``,
        ``"jumphost"``, ``"verify"``, ``"rollback"``, or ``"queued"``
        for the GUI-level wait state.
    status:
        One of ``"start"`` | ``"ok"`` | ``"error"`` | ``"info"``.
    host:
        Optional host name this event refers to (when the phase is
        parallelised across hosts).
    detail:
        Human-readable, user-facing message. Safe to render verbatim
        in a log viewer.
    elapsed_ms:
        Time since ``start`` for this phase/host tuple. ``0`` on
        ``start`` events.
    data:
        Free-form structured data (e.g. per-phase results). Kept
        small so callbacks can serialize to JSON cheaply.
    timestamp:
        Event creation time (``time.time()``). Useful for GUIs that
        display age.
    """

    phase: str
    status: str
    host: str | None = None
    detail: str = ""
    elapsed_ms: int = 0
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "status": self.status,
            "host": self.host,
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
            "data": self.data,
            "timestamp": self.timestamp,
        }


ProgressCallback = Callable[[ProgressEvent], None]


def NullProgress(_event: ProgressEvent) -> None:
    """No-op callback. Used when no progress sink is wired."""
    return None


def log_progress(event: ProgressEvent) -> None:
    """Reference callback that forwards events to the standard logger.

    Useful for tests and for CLI paths that want the same structured
    events the GUI consumes.
    """
    prefix = f"[{event.host}] " if event.host else ""
    msg = f"{prefix}{event.phase}/{event.status}: {event.detail}"
    if event.status == "error":
        log.error(msg)
    elif event.status == "start":
        log.info(msg)
    else:
        log.info(msg)


class _PhaseTimer:
    """Helper that tracks phase start times so elapsed_ms is accurate."""

    def __init__(self, cb: ProgressCallback | None):
        self._cb = cb or NullProgress
        self._starts: dict[tuple[str, str | None], float] = {}

    def emit(
        self,
        phase: str,
        status: str,
        *,
        host: str | None = None,
        detail: str = "",
        data: dict | None = None,
    ) -> ProgressEvent:
        key = (phase, host)
        now = time.time()
        if status == "start":
            self._starts[key] = now
            elapsed_ms = 0
        else:
            start = self._starts.get(key, now)
            elapsed_ms = int((now - start) * 1000)
        evt = ProgressEvent(
            phase=phase,
            status=status,
            host=host,
            detail=detail,
            elapsed_ms=elapsed_ms,
            data=data or {},
            timestamp=now,
        )
        try:
            self._cb(evt)
        except Exception:
            log.exception("Progress callback raised — event dropped")
        return evt


def make_timer(cb: ProgressCallback | None) -> _PhaseTimer:
    """Build a phase timer bound to ``cb`` (or a null sink)."""
    return _PhaseTimer(cb)
