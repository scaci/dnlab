"""Pluggable login backends.

`app.config.settings.AUTH_BACKEND` selects one of:
* ``local_db`` — :class:`.local_db.LocalDbBackend` (default)
* ``ldap``     — :class:`.ldap.LdapBackend` (PR2 stub)
* ``oidc``     — :class:`.oidc.OidcBackend` (PR2 stub)

Use :func:`.factory.get_backend` to resolve the active backend at
request time.
"""

from app.auth.backends.factory import get_backend

__all__ = ["get_backend"]
