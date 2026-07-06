"""Resolve the active auth backend from configuration."""

from __future__ import annotations

from functools import lru_cache

from app.auth.backends.base import AuthBackend
from app.auth.backends.basic_auth import BasicAuthBackend
from app.auth.backends.ldap import LdapBackend
from app.auth.backends.local_db import LocalDbBackend
from app.auth.backends.oidc import OidcBackend
from app.config import settings

_REGISTRY: dict[str, type[AuthBackend]] = {
    "basic_auth": BasicAuthBackend,
    "local_db":   LocalDbBackend,
    "ldap":       LdapBackend,
    "oidc":       OidcBackend,
}


@lru_cache(maxsize=1)
def get_backend() -> AuthBackend:
    """Return the singleton backend selected by ``settings.AUTH_BACKEND``."""
    name = (settings.AUTH_BACKEND or "local_db").strip().lower()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown AUTH_BACKEND={name!r}. "
            f"Supported: {sorted(_REGISTRY)}",
        )
    return cls()
