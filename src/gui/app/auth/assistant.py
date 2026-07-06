"""Assistant-role helpers."""

from __future__ import annotations

from fastapi import Request, Response, status

from app.auth.backends import get_backend
from app.auth.models import Role


async def block_assistant_gui(request: Request, call_next) -> Response:
    """Allow assistant sessions only on API and API-WebSocket paths."""
    path = request.url.path
    if (
        path.startswith("/api/")
        or path.startswith("/ws/")
        or path in {"/api", "/openapi.json"}
    ):
        return await call_next(request)

    user = await get_backend().resolve_request(
        cookies=request.cookies,
        headers=request.headers,
    )
    if user is not None and user.role == Role.assistant:
        return Response(
            "assistant users are API-only",
            status_code=status.HTTP_403_FORBIDDEN,
            media_type="text/plain",
        )
    return await call_next(request)
