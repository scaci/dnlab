"""FastAPI dependencies for authentication and RBAC.

Two public dependencies:

* :func:`get_current_user` — resolves the active user via the active
  backend's :meth:`resolve_request`. Raises 401 when no valid auth
  material is present. Does NOT open a DB session itself — backends
  that need one open a short-lived session internally so basic_auth
  deployments can run Postgres-less.
* :func:`require_role` — factory returning a dependency that asserts
  the active user's role is *at least* the requested level. Role
  hierarchy (highest → lowest): ``admin > graduate/assistant > student
  > rookie``.

For WebSocket endpoints use :func:`authenticate_ws` — the FastAPI
dependency system doesn't cleanly intersect with WS handshake code
paths, so WS handlers call this explicitly after
``reject_if_bad_origin``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Awaitable, Callable

from fastapi import Depends, HTTPException, Request, WebSocket, status

from app.auth.backends import get_backend
from app.auth.models import Role, User

log = logging.getLogger(__name__)


_ROLE_ORDER = {
    Role.rookie: 0,
    Role.student: 1,
    Role.graduate: 2,
    Role.assistant: 2,
    Role.admin: 3,
}


def _role_satisfies(actual: Role, required: Role) -> bool:
    return _ROLE_ORDER[actual] >= _ROLE_ORDER[required]


async def get_current_user(request: Request) -> User:
    """Resolve the authenticated user or raise 401."""
    backend = get_backend()
    user = await backend.resolve_request(
        cookies=request.cookies,
        headers=request.headers,
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
        )
    return user


def require_role(required: Role) -> Callable[[User], Awaitable[User]]:
    """Return a dependency enforcing ``user.role >= required``."""

    async def _dep(user: Annotated[User, Depends(get_current_user)]) -> User:
        if not _role_satisfies(user.role, required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {required.value} required (have {user.role.value})",
            )
        return user

    _dep.__name__ = f"require_role_{required.value}"
    return _dep


async def authenticate_ws(ws: WebSocket) -> User | None:
    """Resolve the user for a WebSocket handshake, or None if unauth.

    WebSockets in FastAPI don't play nicely with Depends for the
    handshake phase. Call this right after
    :func:`app.security.reject_if_bad_origin`:

        if await reject_if_bad_origin(ws): return
        user = await authenticate_ws(ws)
        if user is None:
            await ws.close(code=4401); return
        await ws.accept()
    """
    backend = get_backend()
    return await backend.resolve_request(
        cookies=ws.cookies, headers=ws.headers,
    )
