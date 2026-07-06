"""Auth / RBAC stack for dnlab-gui (M7 fase 2).

Submodules:
    db       -- async SQLAlchemy engine & session dependency
    models   -- ORM models (User, Session, AuditEvent)
    backends -- pluggable login backends (PR 2)
    sessions -- opaque DB-backed session token helpers (PR 2)
    audit    -- audit-log writer (PR 2)
"""
