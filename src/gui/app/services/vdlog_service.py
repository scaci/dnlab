"""Stream VD container logs through the runtime relay."""

from __future__ import annotations

import logging

from fastapi import WebSocket

from app.services.runtime_relay_client import RuntimeRelayClient

log = logging.getLogger(__name__)

DEFAULT_TAIL_LINES = 200


class VdLogService:
    """Stream ``docker logs -f`` for a VD container through the relay."""

    async def stream(
        self,
        websocket: WebSocket,
        relay: dict,
        *,
        tail: int = DEFAULT_TAIL_LINES,
    ) -> None:
        await websocket.send_text(
            f"[Streaming docker logs for {relay['container']} via runtime relay]\r\n"
        )
        try:
            await RuntimeRelayClient().stream_logs(
                websocket,
                relay,
                tail=tail,
                follow=True,
            )
        finally:
            try:
                await websocket.send_text("[Stream terminato]\r\n")
            except Exception:
                pass
