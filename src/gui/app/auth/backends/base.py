"""Abstract protocol every auth backend implements.

Two methods:

* :meth:`authenticate` — credential check, used by ``POST /api/auth/login``.
  Only meaningful for cookie-based backends (``local_db``, ``ldap``);
  header-based backends (``basic_auth``, ``oidc``) raise
  ``NotImplementedError``.
* :meth:`resolve_request` — per-request auth lookup. Called from
  :func:`app.auth.deps.get_current_user` on every protected endpoint.
  Default implementation is cookie+DB session; header-based backends
  override it.

The default :meth:`resolve_request` opens its own short-lived DB
session so HTTP handlers don't need ``Depends(get_session)`` just to
authenticate — important because ``basic_auth`` installations may run
without Postgres at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Mapping

from app.auth.models import User


class AuthBackend(ABC):
    """Minimum contract a login backend must satisfy.

    The DB-backed :class:`~app.auth.models.User` row is still the
    source of truth for role and is_active — even federated backends
    (ldap, oidc) rely on it to decide RBAC. basic_auth is the
    exception: it issues *ephemeral* User objects synthesized from
    the upstream header, so it needs no DB at all.
    """

    name: str  # must match a value of app.auth.models.AuthBackend

    @abstractmethod
    async def authenticate(
        self,
        db,
        *,
        username: str,
        password: str,
    ) -> User | None:
        """Check credentials. Return the User row on success, ``None`` otherwise.

        Implementations should NOT leak *why* auth failed (missing
        user vs. wrong password vs. inactive) — always return ``None``.
        The caller logs a generic ``login.failure`` audit event.
        """

    async def resolve_request(
        self,
        *,
        cookies: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> User | None:
        """Return the authenticated user for a request, or None.

        Default: cookie-based session lookup against the DB. Header-
        based backends (``basic_auth``) override this and ignore the
        DB entirely.

        Implementations receive dict-like cookies/headers so the same
        entry point works for both HTTP requests and WebSocket
        handshakes. HTTP handlers can pass
        ``request.cookies`` / ``request.headers`` directly.
        """
        # Lazy import to keep basic_auth-only deployments from paying
        # the SQLAlchemy import cost on every request.
        from app.auth.db import AsyncSessionLocal
        from app.auth.sessions import load_session
        from app.config import settings

        token = cookies.get(settings.SESSION_COOKIE_NAME)
        if not token:
            return None
        async with AsyncSessionLocal() as db:
            loaded = await load_session(db, token=token)
            if loaded is None:
                return None
            await db.commit()  # persist last_seen_at bump
            _session, user = loaded
            return user
