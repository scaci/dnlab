"""OpenID Connect backend — stub reserved for a later iteration.

Shape for when it's implemented: a browser redirect flow (authorization
code + PKCE), not a username/password POST. That means the
``authenticate`` contract here is the wrong shape; when we wire OIDC
in, we'll add a second pair of endpoints (``/api/auth/oidc/start``,
``/api/auth/oidc/callback``) and keep this class purely as a marker
for ``backend = oidc`` User rows.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backends.base import AuthBackend
from app.auth.models import User


class OidcBackend(AuthBackend):
    name = "oidc"

    async def authenticate(
        self,
        db: AsyncSession,
        *,
        username: str,
        password: str,
    ) -> User | None:
        raise NotImplementedError(
            "OIDC uses a redirect flow, not password POST. "
            "Set DNLABGUI_AUTH_BACKEND=local_db or wait for a later PR.",
        )
