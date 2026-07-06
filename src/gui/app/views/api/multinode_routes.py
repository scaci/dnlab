"""REST + WebSocket routes for the multinode orchestrator backend.

Two groups of endpoints:

* **Site-wide / admin-only**
    - ``GET  /api/hosts/``                    — site-wide host inventory
    - ``GET  /api/image-sync/status``         — image-sync daemon state
    - ``POST /api/image-sync/reconcile``      — **admin-only**: fire
      SIGUSR1 at the image-sync systemd unit

* **Per-lab**
    - ``GET  /api/labs/{lab_id}/plan``        — scheduling plan
    - ``GET  /api/labs/{lab_id}/status-live`` — live status probe
    - ``POST /api/labs/{lab_id}/sync-images`` — push images to workers
    - ``WS   /ws/events/{lab_id}``            — progress event stream

Per-lab routes resolve the UUID into a :class:`ResolvedLab` via
:mod:`app.services.lab_resolver`, enforcing the read/write authz matrix
(read for plan/status, write for sync-images because it kicks off side
effects on workers).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.auth.db import AsyncSessionLocal, get_session
from app.auth.deps import authenticate_ws, get_current_user, require_role
from app.auth.models import Role, User
from app.security import reject_if_bad_origin
from app.services.events_bus import bus
from app.services.lab_resolver import resolve_for_read, resolve_for_write, resolve_ws
from app.services.multinode_service import MultinodeServiceError, multinode
from app.services.shutdown_registry import shutdown_registry

log = logging.getLogger(__name__)

router = APIRouter(tags=["multinode"])


# ── REST: site-wide ────────────────────────────────────────────────

@router.get("/api/hosts/")
async def list_hosts(user: Annotated[User, Depends(get_current_user)]):
    try:
        return await multinode.list_hosts()
    except MultinodeServiceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/image-sync/status")
async def image_sync_status(
    user: Annotated[User, Depends(get_current_user)],
):
    state = await multinode.image_sync_status()
    if state is None:
        return {"available": False}
    return {"available": True, "state": state}


@router.post("/api/image-sync/reconcile")
async def image_sync_trigger_reconcile(
    user: Annotated[User, Depends(require_role(Role.admin))],
):
    """Wake the image-sync daemon and ask for an immediate reconcile.

    Admin-only: the daemon pushes to every worker and can saturate
    egress bandwidth; restrict the trigger to operators that own the
    infrastructure.
    """
    log.info("POST /api/image-sync/reconcile (user=%s)", user.username)
    try:
        result = await multinode.trigger_image_sync_reconcile()
    except MultinodeServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return result


# ── REST: per-lab ──────────────────────────────────────────────────

@router.get("/api/labs/{lab_id}/plan")
async def lab_plan(
    lab_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    lab = await resolve_for_read(db, lab_id, user)
    try:
        return await multinode.plan(lab)
    except MultinodeServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        log.warning("plan %s failed: %s", lab.netname, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/labs/{lab_id}/status-live")
async def lab_status_live(
    lab_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    lab = await resolve_for_read(db, lab_id, user)
    try:
        report = await multinode.status(lab, emit_events=False)
    except MultinodeServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Swap netname for display name so the UI shows the right label.
    report["lab_name"] = lab.display_name
    report["id"] = str(lab.id)
    return report


@router.get("/api/labs/{lab_id}/nodes")
async def lab_nodes(
    lab_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    lab = await resolve_for_read(db, lab_id, user)
    try:
        return {"nodes": await multinode.node_list(lab)}
    except MultinodeServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/labs/{lab_id}/jumphost/password")
async def lab_jumphost_password(
    lab_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Return the per-lab jumphost labuser password.

    Read-gated like the rest of the lab (``resolve_for_read``). Access
    is audit-logged but the password itself is never written to logs or
    into the audit detail payload.
    """
    lab = await resolve_for_read(db, lab_id, user)
    try:
        password = await multinode.jumphost_password(lab)
    except MultinodeServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await audit.record(
        db, event="lab.jumphost_password_read", user=user, request=request,
        resource=f"lab:{lab.netname}",
        detail={"lab_id": str(lab.id), "display_name": lab.display_name},
    )
    await db.commit()
    log.info("jumphost password read: lab=%s user=%s", lab.netname, user.username)
    return {"password": password}


@router.post("/api/labs/{lab_id}/sync-images")
async def lab_sync_images(
    lab_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    # sync-images triggers side-effects on every worker — treat as
    # write-equivalent.
    lab = await resolve_for_write(db, lab_id, user)
    log.info("POST /api/labs/%s/sync-images (user=%s)", lab_id, user.username)
    try:
        return await multinode.sync_images(lab)
    except MultinodeServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ── WebSocket ──────────────────────────────────────────────────────

@router.websocket("/ws/events/{lab_id}")
async def ws_events(ws: WebSocket, lab_id: UUID):
    """Stream :class:`BusEvent`s for ``lab_id`` to the client as JSON.

    Auth: 4401 if not authenticated, 4404 if lab not found / no read
    permission. On connect we replay the ring-buffer history, then
    stream new events as they arrive.
    """
    if await reject_if_bad_origin(ws):
        return
    user = await authenticate_ws(ws)
    if user is None:
        await ws.close(code=4401)
        return
    async with AsyncSessionLocal() as db:
        lab = await resolve_ws(db, lab_id, user)
    if lab is None:
        await ws.close(code=4404)
        return

    await ws.accept()
    topic = str(lab.id)
    q = bus.subscribe(topic, replay=True)
    log.info("ws/events/%s (display=%s): subscriber connected (%d total)",
             topic, lab.display_name, bus.subscriber_count(topic))
    label = f"ws/events/{lab_id}"
    async with shutdown_registry.track(label):
        try:
            while True:
                evt = await q.get()
                await ws.send_json(evt.to_dict())
        except (WebSocketDisconnect, asyncio.CancelledError):
            log.info("ws/events/%s: subscriber disconnected", topic)
        except Exception:
            log.exception("ws/events/%s: unexpected error", topic)
        finally:
            bus.unsubscribe(topic, q)
            try:
                await ws.close()
            except Exception:
                pass
