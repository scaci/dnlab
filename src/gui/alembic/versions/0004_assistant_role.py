"""add assistant role

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'assistant' "
            "AFTER 'graduate'",
        )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_single_local_assistant "
        "ON users (role) "
        "WHERE role = 'assistant' AND backend = 'local_db'",
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_users_single_local_assistant")
    # Postgres cannot drop enum values without recreating the type.
