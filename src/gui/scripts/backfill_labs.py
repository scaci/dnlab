#!/usr/bin/env python3
"""One-shot migration: register pre-existing topology YAMLs as Lab rows.

Before M7 the GUI identified labs by filename stem. M7 introduces a
UUID primary key in the ``labs`` table whose 12-char sha-derived form
becomes the clab topology ``name:`` (and therefore the Docker network
name). This script walks ``TOPOLOGIES_DIR``, inserts a Lab row for each
YAML it finds, and rewrites the file on disk so runtime state matches
the DB.

Per-file steps:

1. Skip files whose stem is already a UUID (re-runnable).
2. Read the top-level ``name:`` key — kept as the user-facing display
   name on the Lab row.
3. Insert a ``labs`` row (owner = first admin from DB; see
   ``scripts/seed_admin.py``).
4. Rewrite ``name:`` inside the YAML to the derived netname.
5. Rename ``<display>.yml`` to ``<uuid>.yml``.
6. Move sidecars that followed the old stem:
     - ``<display>.yml.annotations.json``  →  ``<uuid>.yml.annotations.json``
     - ``configs/<display>/``              →  ``configs/<netname>/``

PyYAML round-trips lose comments/ordering — this is a one-shot
migration on files the GUI itself produces, so that trade is
acceptable. Preserving layout would require ruamel.yaml, which isn't
installed.

Invocation:
    python -m scripts.backfill_labs --dry-run      # preview, no writes
    python -m scripts.backfill_labs                # apply

Run with the auth DB reachable (source deploy/auth/.env first).
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from uuid import UUID

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.auth.db import AsyncSessionLocal  # noqa: E402
from app.auth.labs import derive_network_name  # noqa: E402
from app.auth.models import Lab, Role, User  # noqa: E402
from app.config import settings  # noqa: E402


def _is_uuid_stem(stem: str) -> bool:
    try:
        UUID(stem)
    except ValueError:
        return False
    return True


def _iter_topology_yamls(root: Path):
    for ext in ("*.yml", "*.yaml"):
        for p in sorted(root.glob(ext)):
            if p.is_file():
                yield p


async def _first_admin(db) -> User:
    stmt = select(User).where(User.role == Role.admin).order_by(User.id)
    admin = (await db.execute(stmt)).scalars().first()
    if admin is None:
        raise SystemExit(
            "[backfill] ERROR: no admin user in DB — run scripts/seed_admin.py "
            "before backfilling.",
        )
    return admin


async def _backfill_one(
    db, path: Path, *, owner: User, dry_run: bool,
) -> None:
    if _is_uuid_stem(path.stem):
        print(f"[backfill] skip {path.name} (already UUID-named)")
        return

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        print(f"[backfill] skip {path.name}: YAML parse error: {e}")
        return

    display_name = data.get("name") or path.stem
    if not isinstance(display_name, str) or not display_name:
        print(f"[backfill] skip {path.name}: no usable top-level name")
        return

    existing = (
        await db.execute(
            select(Lab).where(
                Lab.owner_id == owner.id, Lab.name == display_name,
            ),
        )
    ).scalar_one_or_none()
    if existing is not None:
        lab_uuid = existing.id
        print(
            f"[backfill] {path.name}: lab row already exists "
            f"(uuid={lab_uuid}) — ensuring file state matches",
        )
    else:
        import uuid as _uuid
        lab_uuid = _uuid.uuid4()
        print(
            f"[backfill] {path.name}: would create lab "
            f"display={display_name!r} uuid={lab_uuid} owner={owner.username}"
            if dry_run else
            f"[backfill] {path.name}: creating lab "
            f"display={display_name!r} uuid={lab_uuid} owner={owner.username}",
        )
        if not dry_run:
            db.add(Lab(id=lab_uuid, name=display_name, owner_id=owner.id))
            await db.flush()

    netname = derive_network_name(lab_uuid)
    new_yaml = path.with_name(f"{lab_uuid}.yml")

    # Rewrite ``name:`` before renaming so a failure leaves the original
    # content readable under the original path.
    data["name"] = netname
    if dry_run:
        print(f"[backfill]   would rewrite {path.name}: name → {netname}")
        print(f"[backfill]   would rename  {path.name} → {new_yaml.name}")
    else:
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        if new_yaml != path:
            path.rename(new_yaml)

    ann = path.with_name(f"{path.name}.annotations.json")
    if ann.exists():
        target = new_yaml.with_name(f"{new_yaml.name}.annotations.json")
        if dry_run:
            print(f"[backfill]   would rename {ann.name} → {target.name}")
        else:
            ann.rename(target)

    cfg_src = settings.TOPOLOGIES_DIR / "configs" / display_name
    if cfg_src.is_dir():
        cfg_dst = settings.TOPOLOGIES_DIR / "configs" / netname
        if dry_run:
            print(f"[backfill]   would rename configs/{display_name} → configs/{netname}")
        else:
            shutil.move(str(cfg_src), str(cfg_dst))


async def _run(dry_run: bool) -> int:
    root = settings.TOPOLOGIES_DIR
    if not root.is_dir():
        print(f"[backfill] TOPOLOGIES_DIR {root} does not exist — nothing to do.")
        return 0

    files = list(_iter_topology_yamls(root))
    if not files:
        print(f"[backfill] no YAML files under {root} — nothing to do.")
        return 0

    async with AsyncSessionLocal() as db:
        owner = await _first_admin(db)
        print(
            f"[backfill] scanning {root} ({len(files)} file(s)); "
            f"owner for unassigned labs = {owner.username!r} "
            f"{'(dry-run)' if dry_run else ''}",
        )
        for p in files:
            await _backfill_one(db, p, owner=owner, dry_run=dry_run)
        if not dry_run:
            await db.commit()
    print("[backfill] done.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print planned actions without writing to DB or filesystem",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
