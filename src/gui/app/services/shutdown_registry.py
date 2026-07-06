"""Track long-lived sessions so service stop can close them promptly."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

CloseCallback = Callable[[], None | Awaitable[None]]


@dataclass
class _Entry:
    label: str
    task: asyncio.Task
    close_callbacks: list[CloseCallback]


class ShutdownRegistry:
    """Registry for WebSocket/task sessions that can block uvicorn stop.

    Uvicorn waits for open connections before running FastAPI lifespan
    shutdown. We therefore also call :meth:`request_shutdown` from the
    server signal handler, cancelling active route tasks early enough for
    their ``finally`` blocks to reap subprocesses and sockets.
    """

    def __init__(self) -> None:
        self._entries: dict[int, _Entry] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._shutdown_requested = False
        self._next_id = 1

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    @property
    def shutdown_requested(self) -> bool:
        with self._lock:
            return self._shutdown_requested

    def request_shutdown(self, reason: str = "shutdown") -> None:
        with self._lock:
            first = not self._shutdown_requested
            self._shutdown_requested = True
            loop = self._loop
        if first:
            log.info("shutdown_registry: shutdown requested (%s)", reason)
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._cancel_active, reason)

    async def drain(self, *, timeout: float = 5.0) -> None:
        self.request_shutdown("lifespan shutdown")
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            with self._lock:
                tasks = [
                    e.task for e in self._entries.values()
                    if e.task is not asyncio.current_task() and not e.task.done()
                ]
            if not tasks:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                log.warning(
                    "shutdown_registry: %d task(s) still active after %.1fs",
                    len(tasks), timeout,
                )
                return
            await asyncio.wait(tasks, timeout=min(remaining, 0.5))

    @asynccontextmanager
    async def track(self, label: str, *callbacks: CloseCallback):
        task = asyncio.current_task()
        if task is None:
            yield
            return
        token = self.register(label, task=task, callbacks=list(callbacks))
        try:
            if self.shutdown_requested:
                task.cancel()
            yield
        finally:
            self.unregister(token)

    def register(
        self,
        label: str,
        *,
        task: asyncio.Task,
        callbacks: list[CloseCallback] | None = None,
    ) -> int:
        with self._lock:
            token = self._next_id
            self._next_id += 1
            self._entries[token] = _Entry(label, task, callbacks or [])
            shutdown_requested = self._shutdown_requested
        log.debug("shutdown_registry: registered %s", label)
        if shutdown_requested:
            task.cancel()
        return token

    def unregister(self, token: int) -> None:
        with self._lock:
            entry = self._entries.pop(token, None)
        if entry:
            log.debug("shutdown_registry: unregistered %s", entry.label)

    def _cancel_active(self, reason: str) -> None:
        with self._lock:
            entries = list(self._entries.values())
        if not entries:
            return
        log.info(
            "shutdown_registry: cancelling %d active session(s) (%s)",
            len(entries), reason,
        )
        for entry in entries:
            for callback in entry.close_callbacks:
                try:
                    result = callback()
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
                except Exception:
                    log.exception(
                        "shutdown_registry: close callback failed for %s",
                        entry.label,
                    )
            if not entry.task.done():
                entry.task.cancel()


shutdown_registry = ShutdownRegistry()
