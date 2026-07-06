"""ORM models for the auth / RBAC stack.

Four tables:

* ``users``      -- identities (local_db or federated backends).
* ``sessions``   -- opaque server-side session tokens; the cookie is a
                    random 32-byte urlsafe string whose hash is the PK.
                    Storing sessions in the DB lets us revoke on logout
                    or password-change without waiting for cookie TTL.
* ``labs``       -- authoritative lab registry. UUID primary key is
                    used to derive clab's network/bridge names so two
                    users can have labs with the same display name.
                    Unique constraint (owner_id, name) prevents one
                    user from having duplicates.
* ``audit_log``  -- append-only trail of security-relevant events. The
                    ``username`` column is frozen at event time so log
                    entries stay meaningful even if the user is later
                    renamed or deleted.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.auth.db import Base


class Role(str, enum.Enum):
    """RBAC roles. Ordering in the enum reflects privilege level.

    Hierarchy (highest → lowest):

    * ``admin``    -- full RW on every lab, access to admin-only areas
                      (image-sync reconcile, multinode global ops, user
                      management).
    * ``graduate`` -- RW on own labs + RW on every student lab.
                      Read-only on admin and other graduates' labs.
    * ``assistant`` -- API-only actor with the same lab permissions as
                       ``graduate``.
    * ``student``  -- RW on own labs only. Read-only on everything else.
    * ``rookie``   -- read-only everywhere. Cannot own or create labs.
    """

    admin = "admin"
    graduate = "graduate"
    assistant = "assistant"
    student = "student"
    rookie = "rookie"


class AuthBackend(str, enum.Enum):
    """Which backend authenticated the user."""

    basic_auth = "basic_auth"
    local_db = "local_db"
    ldap = "ldap"
    oidc = "oidc"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # argon2id PHC-encoded hash. NULL for federated users (ldap/oidc)
    # where the directory owns the credential.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[Role] = mapped_column(
        SAEnum(Role, name="user_role"), nullable=False, default=Role.rookie,
    )
    backend: Mapped[AuthBackend] = mapped_column(
        SAEnum(AuthBackend, name="auth_backend"),
        nullable=False,
        default=AuthBackend.local_db,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )
    labs: Mapped[list["Lab"]] = relationship(back_populates="owner")


class Session(Base):
    __tablename__ = "sessions"

    # SHA-256 hex digest of the opaque cookie token. We store only the
    # hash, never the plaintext — an attacker with DB read cannot reuse
    # an existing session.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class AuditEvent(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    # NULL when the event has no subject (e.g. anonymous login attempt).
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    # Frozen username at event time — stays stable if user is renamed
    # or deleted. For anonymous events, the attempted login name.
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


Index("ix_audit_log_user_event", AuditEvent.user_id, AuditEvent.event)


class Lab(Base):
    """Authoritative registry for labs managed by the GUI.

    The UUID primary key is the *internal* identifier — it feeds
    :func:`app.auth.labs.derive_network_name` to produce the clab
    topology ``name:`` (12 hex chars, deterministic from the UUID).
    That keeps clab's network/bridge names unique across users even
    when two users pick the same ``name`` for their labs.

    ``name`` is the user-facing display name; ``(owner_id, name)`` is
    unique so one user cannot have two labs with the same display
    name, but different users can share a display name without
    collision.

    ``owner_id`` is nullable so backfill / unassigned rows stay valid.
    Authz treats ``owner_id IS NULL`` as admin-owned by convention.
    """

    __tablename__ = "labs"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    owner: Mapped["User | None"] = relationship(back_populates="labs")

    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_labs_owner_name"),
    )
