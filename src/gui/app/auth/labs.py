"""Lab identity and per-row authorization.

Two concerns in one module:

* **Naming** — :func:`derive_network_name` turns a lab UUID into a
  deterministic 12-char hex identifier used as the clab topology
  ``name:`` (and therefore as the Docker network name). The bridge
  name is always ``br-<netname>`` and fits in Linux's 15-char
  IFNAMSIZ limit with one char to spare.

* **Authorization** — :func:`can_read_lab` and :func:`can_write_lab`
  encode the matrix confirmed with the user:

  ======= ====== ========= ======= ========
  Actor      admin  graduate/assistant  student rookie
  =======    ====== ==================  ======= ========
  admin      RW     RW                  RW      RW
  grad/asst  R      R (own)             RW      R
  student    R      R                   R (own) R
  rookie     R      R                   R       R
  =======    ====== ==================  ======= ========

  ``basic_auth`` users are treated as super-admin (bypass) since the
  backend exists specifically for dev/test where ownership semantics
  don't apply.
"""

from __future__ import annotations

import hashlib
import uuid as uuidlib
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.models import AuthBackend as BackendEnum, Lab, Role, User

# 12 hex chars = 48 bits of entropy. Birthday-bound at 2^24 ≈ 16M labs
# before 50% collision chance — vastly higher than any realistic usage.
_NETNAME_HEX_CHARS = 12
_BRIDGE_PREFIX = "br-"


def derive_network_name(lab_id: uuidlib.UUID | str) -> str:
    """Deterministic 12-char network name derived from a lab UUID."""
    if isinstance(lab_id, str):
        lab_id = uuidlib.UUID(lab_id)
    return hashlib.sha256(lab_id.bytes).hexdigest()[:_NETNAME_HEX_CHARS]


def derive_bridge_name(lab_id: uuidlib.UUID | str) -> str:
    """Deterministic Linux bridge name (``br-<netname>``) for a lab UUID.

    15 chars total — exactly within Linux IFNAMSIZ (16 with NUL).
    """
    return _BRIDGE_PREFIX + derive_network_name(lab_id)


# ── Authorization matrix ─────────────────────────────────────────────

def _is_super_admin(actor: User) -> bool:
    """basic_auth ephemeral users bypass ownership; so do admins."""
    if actor.backend == BackendEnum.basic_auth:
        return True
    return actor.role == Role.admin


def can_read_lab(actor: User, lab: Lab) -> bool:
    """Everyone authenticated can read every lab."""
    return True


def can_write_lab(actor: User, lab: Lab, owner: User | None) -> bool:
    """Return True iff ``actor`` may modify or delete ``lab``.

    ``owner`` is the User row referenced by ``lab.owner_id`` (or None
    when unassigned). Callers should load it once and pass it in —
    avoids a hidden DB round-trip inside this pure-function predicate.
    """
    if _is_super_admin(actor):
        return True

    # Rookies are strictly read-only everywhere.
    if actor.role == Role.rookie:
        return False

    # Unassigned labs are effectively admin-owned; non-admins can't write.
    if owner is None:
        return False

    # Owner can always modify own labs.
    if owner.id == actor.id:
        return True

    # Graduates and API-only assistants may modify any student's lab.
    if actor.role in (Role.graduate, Role.assistant) and owner.role == Role.student:
        return True

    return False


def can_create_lab(actor: User) -> bool:
    """Return True iff ``actor`` may create a new lab.

    Rookies cannot create; everyone else can (labs get owned by the
    creator, so graduates and students produce their own).
    """
    if _is_super_admin(actor):
        return True
    return actor.role in (Role.graduate, Role.assistant, Role.student)


# ── DB helpers ───────────────────────────────────────────────────────

async def get_lab(db: AsyncSession, lab_id: uuidlib.UUID) -> Lab | None:
    return await db.get(Lab, lab_id)


async def get_lab_by_owner_name(
    db: AsyncSession, *, owner_id: int | None, name: str,
) -> Lab | None:
    stmt = select(Lab).where(Lab.owner_id == owner_id, Lab.name == name)
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_all_labs(db: AsyncSession) -> Sequence[Lab]:
    """All labs, ordered by owner then name. Used by list endpoints.

    Eagerly loads ``Lab.owner`` so callers can read the relationship
    without triggering a lazy-load inside the async request context
    (which raises MissingGreenlet).
    """
    stmt = (
        select(Lab)
        .options(selectinload(Lab.owner))
        .order_by(Lab.owner_id.nullsfirst(), Lab.name)
    )
    return (await db.execute(stmt)).scalars().all()


async def create_lab(
    db: AsyncSession,
    *,
    name: str,
    owner: User | None,
    description: str | None = None,
) -> Lab:
    """Insert a new Lab row with a freshly generated UUID."""
    lab = Lab(
        id=uuidlib.uuid4(),
        name=name,
        owner_id=owner.id if owner else None,
        description=description,
    )
    db.add(lab)
    await db.flush()
    return lab
