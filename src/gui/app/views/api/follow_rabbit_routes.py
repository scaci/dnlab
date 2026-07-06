"""Follow the Rabbit Plus API routes."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.auth.db import get_session
from app.auth.deps import get_current_user
from app.auth.models import User
from app.services.lab_resolver import resolve_for_read, resolve_for_write
from app.services.multinode_service import MultinodeServiceError, multinode

log = logging.getLogger(__name__)
router = APIRouter(tags=["follow-rabbit"])


class FollowRabbitStartIn(BaseModel):
    source_node: str = Field(min_length=1)
    src_ip: str = Field(min_length=1)
    dst_ip: str = Field(min_length=1)
    protocol: str | None = None
    src_port: int | None = None
    dst_port: int | None = None
    timeout_seconds: int | None = None


@router.post("/api/labs/{lab_id}/follow-rabbit/sessions")
async def start_follow_rabbit(
    lab_id: UUID,
    req: FollowRabbitStartIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    lab = await resolve_for_write(db, lab_id, user)
    try:
        result = await multinode.follow_rabbit_start(lab, req.model_dump())
    except MultinodeServiceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    await audit.record(
        db,
        event="lab.follow_rabbit.start",
        user=user,
        request=request,
        resource=str(lab.id),
        detail={
            "display_name": lab.display_name,
            "source_node": req.source_node,
            "protocol": req.protocol or "",
            "src_port_present": bool(req.src_port),
            "dst_port_present": bool(req.dst_port),
        },
    )
    await db.commit()
    return result


@router.get("/api/labs/{lab_id}/follow-rabbit/sessions")
async def list_follow_rabbit(
    lab_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    lab = await resolve_for_read(db, lab_id, user)
    try:
        return await multinode.follow_rabbit_sessions(lab)
    except MultinodeServiceError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/api/labs/{lab_id}/follow-rabbit/sessions/{session_id}")
async def stop_follow_rabbit(
    lab_id: UUID,
    session_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    lab = await resolve_for_write(db, lab_id, user)
    try:
        result = await multinode.follow_rabbit_stop(lab, session_id)
    except MultinodeServiceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    await audit.record(
        db,
        event="lab.follow_rabbit.stop",
        user=user,
        request=request,
        resource=str(lab.id),
        detail={"display_name": lab.display_name, "session_id": session_id},
    )
    await db.commit()
    return result
