"""Opaque server-side session tokens.

The session cookie carries a 32-byte cryptographically random token
(urlsafe base64, ~43 chars). The DB stores only its SHA-256 hex
digest — an attacker with DB read-only access cannot reuse an
existing session because the cookie plaintext is never persisted.

The row in :class:`app.auth.models.Session` lets us revoke sessions
synchronously on logout, password change, or admin action, without
waiting for the cookie TTL to lapse.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Session, User
from app.config import settings


_TOKEN_BYTES = 32  # 256 bits of entropy


def _token_digest(token: str) -> str:
    """SHA-256 hex digest used as the session PK."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(
    db: AsyncSession,
    *,
    user: User,
    ip_address: str | None = None,
    user_agent: str | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, Session]:
    """Persist a new session row and return (cookie_token, row).

    The plaintext token is *only* returned here. Caller puts it in the
    cookie; it is not recoverable from the DB afterwards.
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    ttl = ttl_seconds if ttl_seconds is not None else settings.SESSION_TTL_SECONDS
    now = datetime.now(tz=timezone.utc)
    row = Session(
        id=_token_digest(token),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(seconds=ttl),
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(row)
    await db.flush()
    return token, row


async def load_session(
    db: AsyncSession, *, token: str,
) -> tuple[Session, User] | None:
    """Return (session, user) if ``token`` matches a non-expired row.

    Also updates ``last_seen_at`` as a cheap side-effect — cheap
    because the PK index makes the row fetch O(1). Callers should
    commit the enclosing transaction so the timestamp bump sticks.
    """
    if not token:
        return None
    now = datetime.now(tz=timezone.utc)
    digest = _token_digest(token)
    stmt = select(Session, User).join(User).where(Session.id == digest)
    result = await db.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None
    session, user = row
    if session.expires_at <= now:
        await db.delete(session)
        return None
    if not user.is_active:
        return None
    session.last_seen_at = now
    return session, user


async def revoke_session(db: AsyncSession, *, token: str) -> bool:
    """Delete the session row for ``token``. Returns True if a row was found."""
    digest = _token_digest(token)
    row = await db.get(Session, digest)
    if row is None:
        return False
    await db.delete(row)
    return True


async def revoke_all_for_user(db: AsyncSession, *, user_id: int) -> int:
    """Delete every session for ``user_id``. Used on password reset / disable."""
    stmt = select(Session).where(Session.user_id == user_id)
    result = await db.execute(stmt)
    rows = list(result.scalars())
    for row in rows:
        await db.delete(row)
    return len(rows)
