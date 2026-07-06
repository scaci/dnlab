"""UUID ↔ netname ↔ display-name resolver for lab-scoped routes.

Routes path-param labs by UUID (`/api/labs/<uuid>/...`) but the rest of
the stack speaks three different identifiers for the same lab:

* **display_name** — the user-facing string in the UI (e.g. ``"demo"``).
  Stored in ``labs.name``. Two users may pick the same display name.
* **netname** — 12-char sha-derived identifier written as ``name:`` in
  the topology YAML. It drives everything on the wire: clab topology
  name, Docker network, bridge (``br-<netname>``), container names
  (``clab-<netname>-<node>``), per-lab persist dir, jumphost/runtime-relay
  container names, etc. Unique by construction.
* **yaml_path** — ``<TOPOLOGIES_DIR>/<uuid>.yml``.

This module is the single translation point. Route handlers call
:func:`resolve_for_read` / :func:`resolve_for_write` with the URL UUID
and the authenticated user; everything downstream receives the rich
:class:`ResolvedLab` and never touches raw strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select

from app.auth.labs import can_read_lab, can_write_lab, derive_bridge_name, derive_network_name
from app.auth.models import Lab, User
from app.config import settings


@dataclass(frozen=True)
class ResolvedLab:
    """The three identities a route needs in one place."""

    id: UUID
    display_name: str
    netname: str
    bridge: str
    yaml_path: Path
    owner: User | None

    @property
    def exists_on_disk(self) -> bool:
        return self.yaml_path.exists()


def _yaml_path_for(lab_id: UUID) -> Path:
    """Canonical topology path for a UUID: ``<TOPOLOGIES_DIR>/<uuid>.yml``."""
    return settings.TOPOLOGIES_DIR / f"{lab_id}.yml"


def _build(lab: Lab) -> ResolvedLab:
    return ResolvedLab(
        id=lab.id,
        display_name=lab.name,
        netname=derive_network_name(lab.id),
        bridge=derive_bridge_name(lab.id),
        yaml_path=_yaml_path_for(lab.id),
        owner=lab.owner,
    )


async def _load(db: AsyncSession, lab_id: UUID) -> Lab | None:
    """Eager-load the lab + owner so authz never triggers a second trip."""
    stmt = (
        select(Lab)
        .options(selectinload(Lab.owner))
        .where(Lab.id == lab_id)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def resolve_for_read(
    db: AsyncSession, lab_id: UUID, actor: User,
) -> ResolvedLab:
    """Resolve + enforce read permission. Raises 404 / 403."""
    lab = await _load(db, lab_id)
    if lab is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lab not found")
    if not can_read_lab(actor, lab):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "read not allowed")
    return _build(lab)


async def resolve_for_write(
    db: AsyncSession, lab_id: UUID, actor: User,
) -> ResolvedLab:
    """Resolve + enforce write permission. Raises 404 / 403."""
    lab = await _load(db, lab_id)
    if lab is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lab not found")
    if not can_write_lab(actor, lab, lab.owner):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "write not allowed for this lab",
        )
    return _build(lab)


async def resolve_ws(
    db: AsyncSession, lab_id: UUID, actor: User,
) -> ResolvedLab | None:
    """Read-path resolve for WebSocket handshakes — no exception raising.

    Returns None on miss or denied so the caller can ``ws.close(code)``
    with the right code; FastAPI's HTTPException doesn't translate to
    a handshake close frame.
    """
    lab = await _load(db, lab_id)
    if lab is None or not can_read_lab(actor, lab):
        return None
    return _build(lab)
