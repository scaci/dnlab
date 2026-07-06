"""Small HTTP control surface for the image-sync daemon.

Before dockerization the GUI reached the daemon directly (state file on disk +
``systemctl kill -s SIGUSR1`` for an on-demand reconcile). With the daemon
running in its own container that no longer works across container boundaries,
so the daemon now publishes a tiny HTTP API on the internal network:

* ``GET  /health``     — liveness probe (used by the compose healthcheck).
* ``GET  /status``     — the last published state (``read_state_file``).
* ``POST /reconcile``  — wake the daemon for an immediate reconcile pass.

``dnlab-multinode`` proxies ``/image-sync/*`` to this service (see
``dnlab_multinode.api``); the GUI is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from dnlab_multinode.services.image_sync import ImageSyncDaemon, read_state_file

log = logging.getLogger(__name__)


def build_app(daemon: ImageSyncDaemon) -> FastAPI:
    """Build the control app bound to a running :class:`ImageSyncDaemon`."""
    app = FastAPI(title="dnlab-image-sync", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        state = await asyncio.to_thread(read_state_file, daemon.state_file)
        if state is None:
            return {"available": False}
        return {"available": True, "state": state}

    @app.post("/reconcile")
    async def reconcile() -> dict[str, Any]:
        daemon.trigger_reconcile()
        return {"triggered": True}

    return app


def serve_in_thread(daemon: ImageSyncDaemon, host: str, port: int) -> threading.Thread:
    """Run the control app with uvicorn in a daemon thread and return it."""
    import uvicorn

    app = build_app(daemon)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    # install_signal_handlers only works on the main thread; the daemon owns
    # SIGINT/SIGTERM/SIGUSR1, so disable uvicorn's handlers here.
    server.install_signal_handlers = lambda: None

    thread = threading.Thread(
        target=server.run, daemon=True, name="image-sync-http"
    )
    thread.start()
    log.info("image-sync control API listening on %s:%d", host, port)
    return thread
