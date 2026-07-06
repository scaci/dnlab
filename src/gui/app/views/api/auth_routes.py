"""Authentication API: login, logout, whoami.

Endpoint semantics vary with the active backend
(``settings.AUTH_BACKEND``):

* ``local_db`` — ``POST /login`` validates credentials, issues a
  session cookie. ``POST /logout`` revokes it. ``GET /whoami`` reads
  the cookie.
* ``basic_auth`` — the reverse proxy has already authenticated the
  browser, so ``POST /login`` returns 400 (Apache's prompt is what
  should be used). ``POST /logout`` is likewise a 400 — the caller
  has to close the browser to clear cached Basic credentials.
  ``GET /whoami`` reads the ``X-Remote-User`` header.
* ``ldap`` / ``oidc`` — stubs; ``/login`` returns 501 until wired.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.auth.backends import get_backend
from app.auth.db import get_session
from app.auth.deps import get_current_user
from app.auth.models import User
from app.auth.sessions import create_session, load_session, revoke_session
from app.config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    username: str
    role: str
    email: str | None = None
    # Exposed so the frontend can hide the login form / logout button
    # when the active backend does its own auth handling (basic_auth).
    backend: str


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,      # HTTPS-only; Apache reverse proxy terminates TLS
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        path="/",
    )


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> UserOut:
    backend = get_backend()
    try:
        user = await backend.authenticate(
            db, username=body.username, password=body.password,
        )
    except NotImplementedError as e:
        # basic_auth / oidc don't authenticate via POST.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    if user is None:
        await audit.record(
            db, event="login.failure", username=body.username, request=request,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )

    token, _ = await create_session(
        db,
        user=user,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await audit.record(db, event="login.success", user=user, request=request)
    await db.commit()

    _set_session_cookie(response, token)
    log.info("login.success user=%s role=%s", user.username, user.role.value)
    return UserOut(
        username=user.username,
        role=user.role.value,
        email=user.email,
        backend=settings.AUTH_BACKEND,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    # basic_auth has no server-side state to revoke — the browser
    # caches HTTP Basic credentials until it's closed. Signal that
    # server-side logout is unsupported so the frontend shows the
    # correct instructions.
    if settings.AUTH_BACKEND == "basic_auth":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="logout unsupported with basic_auth; close the browser "
                   "to clear cached credentials.",
        )
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if token:
        loaded = await load_session(db, token=token)
        user = loaded[1] if loaded else None
        if await revoke_session(db, token=token):
            await audit.record(db, event="logout", user=user, request=request)
    await db.commit()
    _clear_session_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/whoami", response_model=UserOut)
async def whoami(user: Annotated[User, Depends(get_current_user)]) -> UserOut:
    return UserOut(
        username=user.username,
        role=user.role.value,
        email=user.email,
        backend=settings.AUTH_BACKEND,
    )
