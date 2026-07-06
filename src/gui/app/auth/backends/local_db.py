"""Local DB backend: argon2id verification against the users table."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backends.base import AuthBackend
from app.auth.models import AuthBackend as BackendEnum, User
from app.auth.password import hash_password, needs_rehash, verify_password

log = logging.getLogger(__name__)


class LocalDbBackend(AuthBackend):
    name = "local_db"

    async def authenticate(
        self,
        db: AsyncSession,
        *,
        username: str,
        password: str,
    ) -> User | None:
        stmt = select(User).where(User.username == username)
        user = (await db.execute(stmt)).scalar_one_or_none()
        if user is None or not user.is_active:
            return None
        if user.backend != BackendEnum.local_db or not user.password_hash:
            # User exists but is owned by a federated backend — local
            # auth must not accept a password it cannot have set.
            return None
        if not verify_password(user.password_hash, password):
            return None

        user.last_login_at = datetime.now(tz=timezone.utc)

        # Opportunistic rehash when Argon2 parameters have been raised.
        try:
            if needs_rehash(user.password_hash):
                user.password_hash = hash_password(password)
                log.info("rehashed password for user=%s", user.username)
        except Exception:
            log.exception("rehash attempt failed for user=%s", user.username)

        return user
