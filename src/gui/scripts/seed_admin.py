#!/usr/bin/env python3
"""Seed the first admin user into an empty auth DB.

Runs after ``alembic upgrade head`` during ``scripts/setup-auth-db.sh``.
Exits silently (rc=0) when the DB already has at least one user — the
bootstrap script is idempotent, so re-runs must not prompt again.

When ``users`` is empty AND the active backend is ``local_db``, this
prompts for a username + password, hashes with argon2id, and inserts a
row with ``role=admin`` and ``backend=local_db``. For other backends we
skip — ldap/oidc users are provisioned lazily at first login, and
basic_auth uses ephemeral users with no DB presence.

Invocation:
    python -m scripts.seed_admin             # interactive
    python -m scripts.seed_admin --username alice --password s3cret
    python -m scripts.seed_admin --force     # seed even with users>0
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

# Make ``app`` importable when invoked as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.auth.db import AsyncSessionLocal  # noqa: E402
from app.auth.models import AuthBackend, Role, User  # noqa: E402
from app.auth.password import hash_password  # noqa: E402
from app.config import settings  # noqa: E402


def _prompt_credentials() -> tuple[str, str]:
    """Interactive username + confirmed-password prompt.

    Keeps re-prompting for the password until the confirmation matches,
    so a typo doesn't silently produce an un-recoverable admin row.
    """
    print("[seed-admin] no users in DB — creating the first admin.")
    username = input("  username: ").strip()
    if not username:
        print("[seed-admin] ERROR: username cannot be empty", file=sys.stderr)
        sys.exit(2)
    while True:
        password = getpass.getpass("  password: ")
        confirm = getpass.getpass("  confirm : ")
        if password and password == confirm:
            return username, password
        print("  passwords empty or do not match, try again.")


async def _seed(username: str | None, password: str | None, force: bool) -> int:
    if settings.AUTH_BACKEND != "local_db":
        print(
            f"[seed-admin] AUTH_BACKEND={settings.AUTH_BACKEND} — "
            "skipping (only local_db needs a seeded admin).",
        )
        return 0

    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(func.count(User.id)))).scalar_one()
        if total and not force:
            print(f"[seed-admin] {total} user(s) already present — skipping.")
            return 0

        if username is None or password is None:
            if not sys.stdin.isatty():
                print(
                    "[seed-admin] ERROR: no TTY and no --username/--password "
                    "given; cannot prompt for the first admin.",
                    file=sys.stderr,
                )
                return 3
            username, password = _prompt_credentials()

        existing = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            print(
                f"[seed-admin] user {username!r} already exists — "
                "leaving untouched.",
            )
            return 0

        db.add(User(
            username=username,
            password_hash=hash_password(password),
            role=Role.admin,
            backend=AuthBackend.local_db,
            is_active=True,
        ))
        await db.commit()
        print(f"[seed-admin] created admin user {username!r}.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--username", help="admin username (skips prompt)")
    parser.add_argument("--password", help="admin password (skips prompt)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="create admin even when the users table is non-empty",
    )
    args = parser.parse_args()
    return asyncio.run(_seed(args.username, args.password, args.force))


if __name__ == "__main__":
    sys.exit(main())
