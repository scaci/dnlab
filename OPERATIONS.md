# dNLab Docker Operations Runbook

This runbook describes the target Docker deployment model for dNLab.

## Target Stack

- Expose only `dnlab-proxy` to users.
- Keep `dnlab-gui`, `dnlab-multinode`, `dnlab-image-build` and `dnlab-auth-db`
  on the internal Compose network.
- Run `dnlab-lab-cleanup` as the Docker-native periodic stale-artifact
  reconciler. It uses the same image as `dnlab-multinode`.
- Do not install `dnlab-multinode` in the GUI image.
- Do not mount `/var/run/docker.sock` in the GUI container.
- Keep the DB image vanilla `postgres:16-alpine`; data belongs in volumes and
  operator-managed backups, not in the image.

## Local Fallback Policy

Local Python fallbacks in the GUI codebase are retained only for standalone
developer workflows outside the Docker target. They are not a production path.

In the Docker target:

- `DNLAB_MULTINODE_API_URL` is required;
- `DNLAB_IMAGE_BUILD_API_URL` is required;
- the GUI image must not install the `dnlab-multinode` Python package;
- the GUI must fail API calls if the internal backend is unavailable instead of
  falling back to local orchestrator imports;
- `./smoke.sh` must fail if these invariants are broken.

## Required Secrets

Create a local `.env` from `.env.example` and set at least:

```text
POSTGRES_PASSWORD=<long random value>
```

For production TLS also set:

```text
DNLAB_PROXY_SERVER_NAME=dnlab.example.com
DNLAB_PROXY_WEBUI_SUFFIX=dnlab.example.com
DNLAB_PROXY_HTTPS_PORT=443
DNLAB_PROXY_TLS_DIR=/etc/ssl/dnlab
DNLABGUI_ALLOWED_ORIGINS=https://dnlab.example.com
DNLABGUI_WEBUI_HOST_SUFFIX=dnlab.example.com
```

Do not store real admin bootstrap passwords in `.env`. Export them only for the
one seed command, then remove them from the shell history if needed.

## Fresh Install

1. Prepare host-side config and directories:

For a single-node install, `/etc/dnlab/hosts.yml` should use `localhost` as the
master and no remote workers:

```yaml
infrastructure:
  master:
    host: localhost
    ssh_user: root
  workers: {}
```

```bash
test -f /etc/dnlab/paths.yml
test -f /etc/dnlab/hosts.yml
mkdir -p /root/dnlab-topologies /var/lib/docker/dnlab-backups /var/log/dnlab-gui /var/log/dnlab-multinode /var/lib/dnlab-lab-cleanup
```

For first rollout, keep the cleanup reconciler in report-only mode until the
first smoke run is clean:

```yaml
lab_cleanup:
  enabled: true
  interval_seconds: 300
  grace_seconds: 600
  dry_run: true
```

2. Create `.env`:

```bash
cp .env.example .env
```

3. Build and start through the proxy:

```bash
docker compose -f compose.yml build
docker compose -f compose.yml up -d dnlab-proxy
./smoke.sh
```

4. Seed the first admin:

```bash
DNLABGUI_BOOTSTRAP_ADMIN_USERNAME=admin \
DNLABGUI_BOOTSTRAP_ADMIN_PASSWORD='<one-time-password>' \
docker compose -f compose.yml --profile seed-admin run --rm dnlab-auth-seed
```

5. Re-run the smoke check:

```bash
./smoke.sh
```

## TLS Mode

The Apache proxy is the default reverse proxy. Nginx is not the initial target.

The TLS directory mounted through `DNLAB_PROXY_TLS_DIR` must contain the files
referenced by `DNLAB_PROXY_CERT_FILE` and `DNLAB_PROXY_CERT_KEY_FILE`.

Start TLS mode with:

```bash
DNLAB_PROXY_SERVER_NAME=dnlab.example.com \
DNLAB_PROXY_WEBUI_SUFFIX=dnlab.example.com \
DNLABGUI_ALLOWED_ORIGINS=https://dnlab.example.com \
DNLABGUI_WEBUI_HOST_SUFFIX=dnlab.example.com \
DNLAB_PROXY_TLS_DIR=/etc/ssl/dnlab \
docker compose -f compose.yml -f compose.tls.yml up -d --force-recreate dnlab-gui dnlab-proxy
```

Then verify:

```bash
docker compose -f compose.yml -f compose.tls.yml exec -T dnlab-proxy apache2ctl configtest
curl -kI https://dnlab.example.com/
```

Tip: for production TLS, use a publicly trusted certificate such as Let's
Encrypt. If the deployment uses wildcard Web UI hostnames, request a
certificate that covers both `dnlab.example.com` and `*.dnlab.example.com`;
Let's Encrypt wildcard certificates require DNS-01 validation.

Wildcard Web UI support requires DNS and certificate coverage for both
`DNLAB_PROXY_SERVER_NAME` and `*.${DNLAB_PROXY_WEBUI_SUFFIX}`. The wildcard is
used by the proxy to receive browser requests for virtual-device Web UIs, for
example `<lab-or-device>.dnlab.example.com`, and route them to the matching
Web UI tunnel exposed by the virtual device.

## Upgrade Procedure

1. Back up the auth DB:

```bash
mkdir -p auth-db-dumps
docker compose -f compose.yml exec -T dnlab-auth-db sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' \
  > auth-db-dumps/dnlab_auth_before_upgrade.sql
```

2. Build updated images:

```bash
docker compose -f compose.yml build
```

3. Recreate via proxy dependency chain:

```bash
docker compose -f compose.yml up -d --force-recreate dnlab-multinode dnlab-lab-cleanup dnlab-image-build dnlab-gui dnlab-proxy
```

4. Run guardrails:

```bash
./smoke.sh
```

After validating the dry-run report, either keep scheduled dry-runs or switch
the reconciler to execution mode in `/etc/dnlab/hosts.yml`:

```yaml
lab_cleanup:
  enabled: true
  interval_seconds: 300
  grace_seconds: 600
  dry_run: false
```

Manual checks:

```bash
docker compose -f compose.yml exec dnlab-lab-cleanup \
  dnlab-lab-cleanup sync --dry-run --json

docker compose -f compose.yml exec dnlab-lab-cleanup \
  dnlab-lab-cleanup sync --execute --json
```

## VD Disk Persistence

Persistent virtual-device overlays are stored below the configured persistence
root, normally `/var/lib/docker/dnlab-backups`. The default backend is
`local-sticky`: dNLab records placement history and prefers to keep persistent
VDs on the same worker, migrating overlays only while the lab is offline.

The `cephfs` backend is experimental and not production-tested. It performs
preflight checks for a shared CephFS mount and marker file, but operators must
validate mount behavior, performance, failure handling and recovery before
using it for important labs.

## Production Hardening Override

Use `compose.hardened.yml` after the base and TLS files when validating a
production-like stack:

```bash
docker compose \
  -f compose.yml \
  -f compose.tls.yml \
  -f compose.hardened.yml \
  up -d --force-recreate dnlab-gui dnlab-proxy
```

Current hardening choices:

- GUI filesystem is read-only, with writable mounts only for configured
  topologies/log/persist paths and tmpfs for `/tmp` and `/run`.
- GUI drops all Linux capabilities.
- GUI and proxy run with `no-new-privileges`.
- Auth DB and image-build also run with `no-new-privileges`.
- `dnlab-multinode` remains privileged in this phase because it is the
  intentionally isolated operational boundary for Docker, ContainerLab and host
  orchestration.
- `dnlab-lab-cleanup` runs from a dedicated slim image (`Dockerfile.cleanup`)
  and reaches every host over SSH, so it needs no local Docker socket or
  containerlab binary to do its work. It must remain internal-only.
- `dnlab-image-build` keeps the Docker socket by design because image builds
  require Docker; keep it internal and do not expose it publicly.

Run smoke with the same compose files:

```bash
COMPOSE_FILES=compose.yml:compose.tls.yml:compose.hardened.yml ./smoke.sh
```

## Regression Guardrails

`./smoke.sh` must pass before promoting a Docker stack change. It checks:

- proxy HTTP reachability;
- GUI container has no Docker socket;
- `dnlab_multinode` package is not installed in the GUI image;
- GUI app import does not load `dnlab_multinode` modules;
- RealNet RR status is served through `dnlab-multinode`;
- `hosts.yml` validation is served through `dnlab-multinode`;
- Docker image discovery is served by `dnlab-multinode`;
- `dnlab-lab-cleanup` is running and has published a state snapshot;
- `vrnetlab/dnlab_frr` resolves to ContainerLab kind `linux`.

Run `./preflight.sh` for a fresh-install validation. It starts an isolated
Compose project on port `18088`, uses a new empty Postgres volume, runs Alembic
migrations through GUI startup, seeds the first admin, verifies login through
the proxy, checks GUI isolation, checks that the GUI image does not install
`dnlab-multinode`, and verifies the image-build API. The project is removed
automatically unless `DNLAB_PREFLIGHT_KEEP=1` is set.

## Backups And Restore

Use `pg_dump`/`psql` for the auth DB. Keep dumps outside images and outside git.
The `auth-db-dumps/` directory is ignored for local operator artifacts.

Restore is an operator action, never a Docker build step:

```bash
docker compose -f compose.yml stop dnlab-gui
docker compose -f compose.yml exec -T dnlab-auth-db sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "drop schema public cascade; create schema public;"'
docker compose -f compose.yml exec -T dnlab-auth-db sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' \
  < auth-db-dumps/dnlab_auth_restore.sql
docker compose -f compose.yml up -d dnlab-proxy
./smoke.sh
```
