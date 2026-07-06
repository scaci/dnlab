"""LDAP / AD backend — stub reserved for a later iteration.

Shape for when it's implemented: operator points us at a directory
server (URI, bind DN, bind password in env / secret store); we bind
with the user's credentials (or search-then-bind), pull group
memberships, map them to a local role, and upsert a ``User`` row with
``backend = ldap`` so the rest of the RBAC stack is unchanged.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backends.base import AuthBackend
from app.auth.models import User


class LdapBackend(AuthBackend):
    name = "ldap"

    async def authenticate(
        self,
        db: AsyncSession,
        *,
        username: str,
        password: str,
    ) -> User | None:
        raise NotImplementedError(
            "LDAP backend is not implemented yet. "
            "Set DNLABGUI_AUTH_BACKEND=local_db or wait for a later PR.",
        )
