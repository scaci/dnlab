"""Capture broker API routes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

from app.auth import audit
from app.auth.db import get_session
from app.auth.deps import get_current_user
from app.auth.models import User
from app.config import settings
from app.services.capture_service import CaptureError, capture_service
from app.services.lab_resolver import resolve_for_read

log = logging.getLogger(__name__)
router = APIRouter(tags=["captures"])
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
_HANDLER_SCRIPT = _SCRIPTS_DIR / "dnlab_capture_handler.py"
_HANDLER_BAT = _SCRIPTS_DIR / "dnlab_capture_handler.bat"


def _capture_http_error(exc: CaptureError) -> HTTPException:
    return HTTPException(409, exc.to_dict())


def _public_base_url(request: Request) -> str:
    configured = settings.PUBLIC_BASE_URL
    if configured:
        return configured.rstrip("/") + "/"

    forwarded_host = _first_header_value(request.headers.get("x-forwarded-host"))
    host = forwarded_host or _first_header_value(request.headers.get("host"))
    if host:
        scheme = (
            _first_header_value(request.headers.get("x-forwarded-proto"))
            or request.url.scheme
            or "http"
        )
        return f"{scheme}://{host}/"

    return str(request.base_url)


def _first_header_value(value: str | None) -> str:
    return (value or "").split(",", 1)[0].strip()


class CaptureLaunchRequest(BaseModel):
    target_id: str
    side: str | None = None
    filter: str = ""
    snaplen: int = 0
    promisc: bool = False


@router.get("/api/labs/{lab_id}/captures/targets")
async def capture_targets(
    lab_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    lab = await resolve_for_read(db, lab_id, user)
    try:
        return {"targets": await capture_service.targets(lab)}
    except CaptureError as exc:
        raise _capture_http_error(exc) from exc


@router.post("/api/labs/{lab_id}/captures/launch")
async def launch_capture(
    lab_id: UUID,
    req: CaptureLaunchRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    lab = await resolve_for_read(db, lab_id, user)
    try:
        result = await capture_service.launch(
            lab=lab,
            user_id=user.id,
            target_id=req.target_id,
            side=req.side,
            bpf_filter=req.filter,
            snaplen=req.snaplen,
            promisc=req.promisc,
            base_url=_public_base_url(request),
        )
    except CaptureError as exc:
        raise _capture_http_error(exc) from exc

    target = result.get("target") or {}
    await audit.record(
        db,
        event="lab.capture_launch",
        user=user,
        request=request,
        resource=str(lab.id),
        detail={
            "display_name": lab.display_name,
            "target_id": target.get("id"),
            "node": target.get("node"),
            "peer": target.get("peer"),
            "iface": target.get("iface"),
            "side": target.get("side"),
            "host": target.get("host"),
            "filter_present": bool(req.filter and req.filter.strip()),
        },
    )
    await db.commit()
    return result


@router.get("/api/labs/{lab_id}/captures/active")
async def active_captures(
    lab_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    lab = await resolve_for_read(db, lab_id, user)
    return {
        "captures": await capture_service.active_captures(
            lab_id=str(lab.id),
            user_id=user.id,
        ),
    }


@router.post("/api/labs/{lab_id}/captures/{session_id}/stop")
async def stop_capture(
    lab_id: UUID,
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    lab = await resolve_for_read(db, lab_id, user)
    try:
        return await capture_service.stop_capture(
            lab_id=str(lab.id),
            user_id=user.id,
            session_id=session_id,
        )
    except CaptureError as exc:
        raise _capture_http_error(exc) from exc


@router.get("/api/captures/handler/download")
async def download_capture_handler(
    user: Annotated[User, Depends(get_current_user)],
    platform: str = "python",
):
    path = _HANDLER_BAT if platform == "windows-bat" else _HANDLER_SCRIPT
    if not path.exists():
        raise HTTPException(404, "capture handler script not found")
    return FileResponse(
        path,
        media_type="application/octet-stream" if path.suffix == ".bat" else "text/x-python",
        filename=path.name,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/captures/{token}/status")
async def capture_status(token: str):
    try:
        return await capture_service.token_status(token)
    except CaptureError as exc:
        return JSONResponse(exc.to_dict(), status_code=200)


@router.get("/api/captures/{token}/stream", name="capture_stream")
async def capture_stream(token: str):
    try:
        stream = await capture_service.open_stream(token)
        return StreamingResponse(
            stream,
            media_type="application/vnd.tcpdump.pcap",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except CaptureError as exc:
        raise _capture_http_error(exc) from exc
