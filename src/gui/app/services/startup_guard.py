"""Startup invariant check for M7 fase 2 lab identity.

After PR4a every lab is identified by a UUID: the YAML file is
``<uuid>.yml`` and the ``name:`` inside is the derived netname. The
backfill script (``scripts/backfill_labs.py``) converts pre-M7 labs.

This module runs once at app startup and warns if:

1. There are topology YAML files whose stem is not a UUID — pre-M7
   artifacts that the orchestrator would happily deploy with a
   display-name-collision-prone lab_name. These are flagged as
   "orphan YAMLs" and must be backfilled.
2. There are ``labs`` rows with no matching YAML on disk — orphan
   rows that would 404 every deploy/destroy. Probably a half-deleted
   lab; safe to ignore but worth logging.

We don't refuse startup — the GUI still serves auth, listing, and
admin views with orphans present. But deploy/destroy on an orphan
YAML would break, so the operator sees a visible warning in the log.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select

from app.auth.db import AsyncSessionLocal
from app.auth.models import Lab
from app.config import settings

log = logging.getLogger(__name__)


def _is_uuid(stem: str) -> bool:
    try:
        UUID(stem)
    except ValueError:
        return False
    return True


async def check_lab_identity_invariant() -> None:
    root = settings.TOPOLOGIES_DIR
    if not root.is_dir():
        return

    yaml_files = [p for p in root.iterdir() if p.suffix in (".yml", ".yaml")]
    orphan_yamls = [p for p in yaml_files if not _is_uuid(p.stem)]
    uuid_stems = {UUID(p.stem) for p in yaml_files if _is_uuid(p.stem)}

    try:
        async with AsyncSessionLocal() as db:
            lab_ids = set(
                (await db.execute(select(Lab.id))).scalars().all()
            )
    except Exception as exc:
        log.warning("startup_guard: cannot query labs table (%s) — skipping", exc)
        return

    orphan_rows = lab_ids - uuid_stems
    orphan_files = uuid_stems - lab_ids

    if orphan_yamls:
        log.warning(
            "startup_guard: %d topology file(s) have non-UUID stems — "
            "run scripts/backfill_labs.py to register them: %s",
            len(orphan_yamls),
            ", ".join(p.name for p in orphan_yamls[:10]),
        )
    if orphan_rows:
        log.warning(
            "startup_guard: %d labs row(s) have no matching YAML on disk: %s",
            len(orphan_rows),
            ", ".join(str(i) for i in list(orphan_rows)[:10]),
        )
    if orphan_files:
        log.warning(
            "startup_guard: %d UUID-named YAML(s) have no labs row: %s",
            len(orphan_files),
            ", ".join(str(i) for i in list(orphan_files)[:10]),
        )
    if not (orphan_yamls or orphan_rows or orphan_files):
        log.info("startup_guard: lab identity invariant holds (%d labs)",
                 len(uuid_stems))
