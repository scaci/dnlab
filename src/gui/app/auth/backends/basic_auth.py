"""Trust-upstream Basic Auth backend.

The reverse proxy (Apache / NGINX) terminates HTTP Basic Auth against
its own credential store and forwards the authenticated username to
the GUI via a trusted header. The app does not validate credentials
itself — its job is just to synthesize an *ephemeral*
:class:`~app.auth.models.User` for RBAC.

Because bind is loopback-only (127.0.0.1), anyone able to reach the
app socket is either the reverse proxy or already root on the box. So
we trust the header unconditionally. Never enable this backend on a
public-interface bind.

Stateless by design: no DB access, no session cookie, no audit rows
keyed to a user row. Perfect for dev/test where spinning up Postgres
is overkill.
"""

from __future__ import annotations

import logging
from typing import Mapping

from app.auth.backends.base import AuthBackend
from app.auth.models import AuthBackend as BackendEnum, Role, User
from app.config import settings

log = logging.getLogger(__name__)


class BasicAuthBackend(AuthBackend):
    name = "basic_auth"

    async def authenticate(
        self,
        db,
        *,
        username: str,
        password: str,
    ) -> User | None:
        # The /api/auth/login POST path is a no-op for this backend:
        # the reverse proxy has already authenticated the browser at
        # the HTTP layer. Surface the misconfiguration rather than
        # silently accepting / rejecting credentials.
        raise NotImplementedError(
            "basic_auth runs behind a reverse proxy; "
            "POST /api/auth/login is not used.",
        )

    async def resolve_request(
        self,
        *,
        cookies: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> User | None:
        # Headers are case-insensitive in HTTP, but FastAPI's
        # ``request.headers`` is case-insensitive too — still, normalize
        # by iterating so custom Mapping implementations don't trip us.
        header_name = settings.BASIC_AUTH_REMOTE_USER_HEADER
        username = headers.get(header_name) or headers.get(header_name.lower())
        if not username:
            return None
        try:
            role = Role(settings.BASIC_AUTH_DEFAULT_ROLE)
        except ValueError:
            log.error(
                "Invalid DNLABGUI_BASIC_AUTH_DEFAULT_ROLE=%r — expected "
                "one of %s. Denying request.",
                settings.BASIC_AUTH_DEFAULT_ROLE,
                [r.value for r in Role],
            )
            return None
        # Ephemeral User — not persisted, id stays None. Audit writes
        # tolerate user_id=None and use the frozen username column.
        return User(
            username=username,
            role=role,
            backend=BackendEnum.basic_auth,
            is_active=True,
        )
