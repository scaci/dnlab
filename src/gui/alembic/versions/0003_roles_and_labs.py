"""rename roles (operator→graduate, viewer→rookie, add student) and add labs table

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-19 13:20:00.000000

Two independent changes bundled because they ship in the same PR:

1. The ``user_role`` enum is reshaped to the new four-tier hierarchy:
   ``admin`` stays; ``operator`` is renamed to ``graduate``; ``viewer``
   is renamed to ``rookie``; and ``student`` is added in the middle.
   ``ALTER TYPE ... RENAME VALUE`` is Postgres 10+ and preserves any
   existing rows pointing at the renamed values.

2. The new ``labs`` table stores the authoritative lab registry used
   for per-row authz. UUID primary key is what derives the clab
   network/bridge names, so two users can have labs with the same
   display name without colliding on the wire.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. user_role enum reshape -----------------------------------
    # RENAME VALUE preserves existing rows; ADD VALUE must run outside
    # a transaction in Postgres so we wrap the ADD in autocommit_block.
    op.execute("ALTER TYPE user_role RENAME VALUE 'operator' TO 'graduate'")
    op.execute("ALTER TYPE user_role RENAME VALUE 'viewer' TO 'rookie'")
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'student' "
            "BEFORE 'rookie'",
        )
    # Default flipped from viewer→rookie (name changed; semantics
    # unchanged: new users with no explicit role are read-only).
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'rookie'")

    # --- 2. labs table ----------------------------------------------
    op.create_table(
        "labs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("owner_id", "name", name="uq_labs_owner_name"),
    )
    op.create_index("ix_labs_owner_id", "labs", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_labs_owner_id", table_name="labs")
    op.drop_table("labs")

    # Enum downgrade: rename back. We cannot drop 'student' cleanly
    # without recreating the type (Postgres has no DROP VALUE), so any
    # rows with role='student' would block. Acceptable for a downgrade
    # path that is essentially "revert to previous schema for forensics".
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'viewer'")
    op.execute("ALTER TYPE user_role RENAME VALUE 'rookie' TO 'viewer'")
    op.execute("ALTER TYPE user_role RENAME VALUE 'graduate' TO 'operator'")
