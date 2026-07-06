"""WebSocket log-streaming route.

Delegates to :class:`~app.services.vdlog_service.VdLogService`, which
streams ``docker logs -f`` through the lab-scoped runtime relay.

Handshake sequence: origin check → :func:`authenticate_ws` →
:func:`resolve_ws` (read authz). Closing codes:

* 4401 — not authenticated
* 4404 — lab not found or read denied
"""

from __future__ import annotations

import logging
import asyncio
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth.db import AsyncSessionLocal
from app.auth.deps import authenticate_ws
from app.security import reject_if_bad_origin
from app.services.lab_resolver import resolve_ws
from app.services import multinode_service as multinode_mod
from app.services.shutdown_registry import shutdown_registry
from app.services.vdlog_service import VdLogService

log = logging.getLogger(__name__)

router = APIRouter(tags=["logs"])
_svc = VdLogService()


@router.websocket("/ws/logs/{lab_id}/{node_name}")
async def logs_ws(websocket: WebSocket, lab_id: UUID, node_name: str):
    if await reject_if_bad_origin(websocket):
        return
    user = await authenticate_ws(websocket)
    if user is None:
        await websocket.close(code=4401)
        return
    async with AsyncSessionLocal() as db:
        lab = await resolve_ws(db, lab_id, user)
    if lab is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    relay = await multinode_mod.multinode.resolve_runtime_relay(lab, node_name)
    host_hint = relay["relay_host"]
    log.info("ws/logs/%s/%s: attached via relay on %s (netname=%s)",
             lab.display_name, node_name, host_hint, lab.netname)
    label = f"ws/logs/{lab_id}/{node_name}"
    async with shutdown_registry.track(label):
        try:
            await _svc.stream(websocket, relay)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.exception("logs_ws crashed")
            try:
                await websocket.send_text(f"\r\n[Error: {exc}]\r\n")
            except Exception:
                pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

