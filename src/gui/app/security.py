"""Hardening helpers (M6).

Centralises the origin allowlist used by:

* the ``CORSMiddleware`` registered in :mod:`app.main`
* the ``Origin`` header check performed at the top of every
  ``/ws/...`` endpoint (events, logs, console).

Browsers always send an ``Origin`` header on cross-origin WebSocket
handshakes, so validating it is the canonical defence against a
malicious site opening a socket to the GUI from the victim's
browser. Non-browser clients (admin tooling, CLI debug) typically omit
the header — we allow that case to keep wscat-style debugging viable.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import WebSocket

from app.config import settings

log = logging.getLogger(__name__)


def _default_origins() -> list[str]:
    """Origins implied by HOST/PORT when no explicit list is given.

    Always includes the two loopback variants so a browser served from
    the GUI itself (same-origin) is accepted. If the operator binds to
    a non-loopback address we include that too — they've opted in.
    """
    port = settings.PORT
    origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }
    host = settings.HOST
    if host and host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        origins.add(f"http://{host}:{port}")
    return sorted(origins)


def allowed_origins() -> list[str]:
    """Resolved origin allowlist, never contains ``*``."""
    raw = settings.ALLOWED_ORIGINS
    if not raw:
        return _default_origins()
    result = [o.strip() for o in raw.split(",") if o.strip() and o.strip() != "*"]
    if not result:
        return _default_origins()
    return result


def is_origin_allowed(origin: str | None) -> bool:
    """Return True if ``origin`` is acceptable for a WebSocket handshake.

    Policy:
      * ``None`` / empty → allowed (non-browser client).
      * Otherwise must exact-match (scheme + host + port) an entry in
        :func:`allowed_origins`.
    """
    if not origin:
        return True
    allowed = allowed_origins()
    if origin in allowed:
        return True
    # Accept equivalent representations (e.g. implicit :80 stripping).
    try:
        o = urlparse(origin)
    except Exception:
        return False
    o_norm = f"{o.scheme}://{o.hostname}:{o.port}" if o.port else f"{o.scheme}://{o.hostname}"
    for entry in allowed:
        try:
            a = urlparse(entry)
        except Exception:
            continue
        a_norm = f"{a.scheme}://{a.hostname}:{a.port}" if a.port else f"{a.scheme}://{a.hostname}"
        if o_norm == a_norm:
            return True
    return False


async def reject_if_bad_origin(ws: WebSocket) -> bool:
    """Close the WS with policy-violation code if Origin is forbidden.

    Returns True when the socket was rejected (caller must return), or
    False when validation passed and the caller may proceed to
    ``await ws.accept()``.
    """
    origin = ws.headers.get("origin")
    if is_origin_allowed(origin):
        return False
    log.warning(
        "WebSocket rejected: origin=%r not in allowlist (path=%s)",
        origin, ws.url.path,
    )
    # 4403 — application-level "forbidden" in the 4000–4999 range.
    await ws.close(code=4403)
    return True
