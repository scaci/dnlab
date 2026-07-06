"""Append-only audit log writer.

Call :func:`record` from any code path that changes auth state or
performs an action we might later need to justify: logins, logouts,
role changes, lab deploy/destroy, etc.

Event names are free-form strings; by convention use dot-separated
``<area>.<action>[.<outcome>]`` — e.g. ``login.success``,
``login.failure``, ``session.revoked``, ``deploy.start``. Keeping a
small vocabulary makes the ``event`` index useful for dashboards.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuditEvent, User

log = logging.getLogger(__name__)


def _client_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


def _user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    return request.headers.get("user-agent")


async def record(
    db: AsyncSession,
    *,
    event: str,
    user: User | None = None,
    username: str | None = None,
    resource: str | None = None,
    request: Request | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Insert one audit-log row. Swallow failures to never break the caller."""
    try:
        row = AuditEvent(
            event=event,
            user_id=user.id if user is not None else None,
            username=username if username is not None else (user.username if user else None),
            resource=resource,
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            detail=detail,
        )
        db.add(row)
        await db.flush()
    except Exception:
        # Audit failure must never mask the primary operation's outcome.
        log.exception("audit.record failed (event=%s user=%s)", event, username)
