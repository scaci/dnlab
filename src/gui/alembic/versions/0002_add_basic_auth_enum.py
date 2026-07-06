"""add basic_auth to auth_backend enum

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-19 12:50:00.000000

ALTER TYPE ... ADD VALUE must run outside a transaction in Postgres,
hence ``op.execute`` with autocommit handled by setting the isolation
level. For clarity we use the same approach Alembic's own docs
recommend: ``ALTER TYPE ... ADD VALUE IF NOT EXISTS``.

basic_auth users are ephemeral (not persisted), so no existing rows
need back-filling.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE auth_backend ADD VALUE IF NOT EXISTS 'basic_auth' BEFORE 'local_db'",
        )


def downgrade() -> None:
    # Postgres has no DROP VALUE for enum types — a clean downgrade
    # would require recreating the type and re-pointing the column.
    # The value is harmless if left in place, so downgrade is a no-op.
    pass
