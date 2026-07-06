#!/usr/bin/env bash
# Bootstrap the dnlab-gui auth DB (M7 fase 2).
#
# Idempotent: re-running is safe. On first run it generates a random
# Postgres password into deploy/auth/.env, brings up the Postgres
# container, waits for healthcheck, and applies all Alembic migrations.
# On subsequent runs it skips the password generation and just brings
# the stack up to the latest migration.
#
# Run from the project root OR from anywhere — the script resolves
# paths from its own location.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd)"
AUTH_DIR="$PROJECT_ROOT/deploy/auth"
ENV_FILE="$AUTH_DIR/.env"
ENV_EXAMPLE="$AUTH_DIR/.env.example"

cd "$AUTH_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "[setup] generating $ENV_FILE with a random password"
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  PASSWORD="$(openssl rand -base64 32 | tr -d '=+/\n')"
  # macOS sed needs -i '' — keep GNU-style for Linux.
  sed -i "s|POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PASSWORD}|" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "[setup] wrote new DB password to $ENV_FILE (chmod 600)"
else
  echo "[setup] $ENV_FILE already exists — skipping password generation"
fi

# Make sure the GUI systemd unit can read the connection URL via
# EnvironmentFile=. Append/replace a DNLABGUI_AUTH_DATABASE_URL line
# derived from the POSTGRES_* credentials above.
# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a
URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT:-5432}/${POSTGRES_DB}"
if grep -q '^DNLABGUI_AUTH_DATABASE_URL=' "$ENV_FILE"; then
  sed -i "s|^DNLABGUI_AUTH_DATABASE_URL=.*|DNLABGUI_AUTH_DATABASE_URL=${URL}|" "$ENV_FILE"
else
  printf '\n# Auto-generated for systemd EnvironmentFile\nDNLABGUI_AUTH_DATABASE_URL=%s\n' "$URL" >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

echo "[setup] starting Postgres via docker compose"
docker compose up -d

echo "[setup] waiting for Postgres to report healthy…"
for _ in $(seq 1 30); do
  status="$(docker inspect -f '{{.State.Health.Status}}' dnlab-auth-postgres 2>/dev/null || echo starting)"
  if [[ "$status" == "healthy" ]]; then
    break
  fi
  sleep 1
done
if [[ "$status" != "healthy" ]]; then
  echo "[setup] ERROR: Postgres did not become healthy (status=$status)" >&2
  docker compose logs --tail 50 postgres >&2 || true
  exit 1
fi
echo "[setup] Postgres healthy."

# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

# Export the connection URL so alembic/env.py picks it up without
# requiring the operator to edit app/config.py defaults.
export DNLABGUI_AUTH_DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT:-5432}/${POSTGRES_DB}"

cd "$PROJECT_ROOT"

if [[ -x "$PROJECT_ROOT/venv/bin/alembic" ]]; then
  ALEMBIC="$PROJECT_ROOT/venv/bin/alembic"
else
  ALEMBIC="alembic"
fi

echo "[setup] applying migrations…"
"$ALEMBIC" upgrade head

# Only local_db needs a seeded admin — other backends either provision
# users lazily (ldap/oidc) or have no DB row at all (basic_auth).
# seed_admin.py is itself idempotent: it exits silently when users>0.
AUTH_BACKEND="${DNLABGUI_AUTH_BACKEND:-local_db}"
if [[ "$AUTH_BACKEND" == "local_db" ]]; then
  if [[ -x "$PROJECT_ROOT/venv/bin/python" ]]; then
    PYTHON="$PROJECT_ROOT/venv/bin/python"
  else
    PYTHON="python3"
  fi
  echo "[setup] checking for admin user…"
  "$PYTHON" "$PROJECT_ROOT/scripts/seed_admin.py"
fi

echo "[setup] done."
