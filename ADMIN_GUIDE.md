# dNLab Admin Guide

This guide is for administrators who deploy and operate the dNLab Docker
distribution stack. It complements [OPERATIONS.md](OPERATIONS.md), which is the
command-focused runbook.

For end-user workflows in the browser, see [USER_GUIDE.md](USER_GUIDE.md).

## Architecture

dNLab exposes one public entrypoint, `dnlab-proxy`. The GUI and backend
services stay on the internal Compose network.

Main services:

- `dnlab-proxy`: Apache reverse proxy for HTTP, TLS, WebSocket and per-device
  Web UI access.
- `dnlab-gui`: FastAPI GUI and browser application.
- `dnlab-multinode`: internal orchestration API for Containerlab, workers and
  runtime state.
- `dnlab-image-sync`: internal image synchronization helper.
- `dnlab-lab-cleanup`: periodic reconciler for stale runtime artifacts.
- `dnlab-image-build`: internal API for image-build jobs and logs.
- `dnlab-auth-db`: PostgreSQL database for local authentication.

The GUI container does not mount `/var/run/docker.sock`; Docker discovery and
orchestration flow through the internal services.

## Host Prerequisites

Use Linux hosts suitable for nested container, network and virtual-device
workloads. The reference baseline is Debian 13 on bare metal with Docker Engine
from Docker's official repository, Docker Compose plugin, Containerlab, cgroup
v2 and root or sudo access for host networking operations.

Expose only proxy ports to users, normally 80 and 443 in production.

## Host Configuration

dNLab expects shared host configuration under `/etc/dnlab`.

Required files:

- `/etc/dnlab/hosts.yml`: master and worker host inventory.
- `/etc/dnlab/paths.yml`: shared paths used by GUI and backend services.

The `master` entry identifies the host that runs the Compose stack and
orchestration services. Worker entries identify hosts that can run lab devices.
For a single-node installation, the same host acts as both master and worker;
use `localhost` as the master and omit remote workers:

```yaml
infrastructure:
  master:
    host: localhost
    ssh_user: root
  workers: {}
```

Common host directories:

```bash
sudo mkdir -p /etc/dnlab /root/dnlab-topologies \
  /var/lib/docker/dnlab-backups /var/log/dnlab-gui \
  /var/log/dnlab-multinode /var/lib/dnlab-image-build /opt/vrnetlab
```

`/opt/vrnetlab` must contain the dNLab vrnetlab tree used by
`dnlab-image-build`. For a fresh host:

```bash
if [ ! -d /opt/vrnetlab/.git ]; then
  sudo git clone --branch dnlab https://github.com/scaci/vrnetlab.git /opt/vrnetlab
else
  git -C /opt/vrnetlab remote -v
  git -C /opt/vrnetlab branch --show-current
fi
```

The Compose stack mounts `/etc/dnlab` read-only into the GUI and internal
services.

## Environment File

Create `.env` from `.env.example` and set a strong database password before
starting the stack.

Important settings:

- `DNLAB_VERSION`: image tag. For this release, use `DNLAB_VERSION=0.1.0`.
- `DNLAB_IMAGE_PREFIX`: image registry prefix, normally `ghcr.io/scaci/`.
- `DNLAB_RUNTIME_IMAGE_PREFIX`: runtime image prefix, normally
  `ghcr.io/scaci/dnlab-`.
- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`: auth DB settings.
- `DNLAB_PROXY_HTTP_PORT`: public HTTP port for non-TLS mode.
- `DNLAB_PROXY_SERVER_NAME`: public GUI hostname in TLS mode.
- `DNLAB_PROXY_WEBUI_SUFFIX`: wildcard suffix used for per-device Web UI hosts.
- `DNLABGUI_ALLOWED_ORIGINS`: browser-facing origin for CORS and WebSocket
  origin checks.
- `DNLABGUI_WEBUI_HOST_SUFFIX`: GUI-side wildcard suffix for device Web UI URLs.
- `DNLAB_TOPOLOGIES_DIR`, `DNLAB_PERSIST_ROOT`, `DNLAB_LOG_DIR_GUI`,
  `DNLAB_LOG_DIR_MULTINODE`, `DNLAB_IMAGE_BUILD_WORKSPACE`: host-side storage
  and log directories.

Do not keep real bootstrap admin passwords in `.env`; export them only for the
single seed command.

## First Install

1. Prepare `/etc/dnlab/hosts.yml`, `/etc/dnlab/paths.yml` and the host
   directories. For a single-node install, set `master.host` to `localhost`
   and leave `workers` empty.
2. Install a TLS certificate for the proxy. For a local test, a self-signed
   certificate under `/etc/ssl/dnlab` is acceptable; production should use a
   publicly trusted certificate.
3. Copy `.env.example` to `.env`; set `POSTGRES_PASSWORD`,
   `DNLAB_PROXY_SERVER_NAME`, `DNLAB_PROXY_WEBUI_SUFFIX`,
   `DNLAB_PROXY_HTTPS_PORT`, `DNLAB_PROXY_TLS_DIR`,
   `DNLABGUI_ALLOWED_ORIGINS` and `DNLABGUI_WEBUI_HOST_SUFFIX`.
4. Start the proxy dependency chain from the published GHCR release images:

```bash
docker compose -f compose.yml -f compose.tls.yml up -d dnlab-proxy
```

5. Run `./smoke.sh` against the HTTPS URL. For a self-signed local test:

```bash
COMPOSE_FILES=compose.yml:compose.tls.yml \
DNLAB_SMOKE_PROXY_URL=https://localhost:8443/ \
DNLAB_SMOKE_CURL_INSECURE=1 \
./smoke.sh
```

6. Seed the first administrator:

```bash
DNLABGUI_BOOTSTRAP_ADMIN_USERNAME=admin \
DNLABGUI_BOOTSTRAP_ADMIN_PASSWORD='<one-time-password>' \
docker compose -f compose.yml --profile seed-admin run --rm dnlab-auth-seed
```

7. Run the HTTPS smoke check again.

See [OPERATIONS.md](OPERATIONS.md) for the full runbook.

## TLS And Wildcard Web UI

The TLS override is `compose.tls.yml`.

```bash
DNLAB_PROXY_SERVER_NAME=dnlab.example.com \
DNLAB_PROXY_WEBUI_SUFFIX=dnlab.example.com \
DNLABGUI_ALLOWED_ORIGINS=https://dnlab.example.com \
DNLABGUI_WEBUI_HOST_SUFFIX=dnlab.example.com \
DNLAB_PROXY_TLS_DIR=/etc/ssl/dnlab \
docker compose -f compose.yml -f compose.tls.yml up -d --force-recreate dnlab-gui dnlab-proxy
```

The TLS directory is mounted inside the proxy container as `/etc/ssl/dnlab`.
It must contain the certificate and key referenced by `DNLAB_PROXY_CERT_FILE`
and `DNLAB_PROXY_CERT_KEY_FILE`.

For production, use a publicly trusted certificate. If per-device Web UI access
uses wildcard hostnames, request a certificate that covers both
`dnlab.example.com` and `*.dnlab.example.com`. Wildcard certificates normally
require DNS-01 validation.

Wildcard Web UI support requires DNS and certificate coverage for:

- `DNLAB_PROXY_SERVER_NAME`, such as `dnlab.example.com`;
- `*.${DNLAB_PROXY_WEBUI_SUFFIX}`, such as `*.dnlab.example.com`.

The proxy receives browser requests for per-device Web UI hostnames and routes
them to the matching Web UI tunnel created by dNLab.

## Authentication And RBAC

The default authentication backend is `local_db`, with Argon2id password hashes
stored in PostgreSQL. Other backends may be configured for reverse-proxy basic
auth, LDAP or OIDC depending on deployment policy.

![Users and roles](docs/images/admin-users-roles.png)

Roles:

- `admin`: full access to all labs and administrator areas.
- `graduate`: can manage own labs and student labs; read-only elsewhere.
- `assistant`: API-only automation role with graduate-like API permissions; it
  cannot use the browser GUI or browser Web UI access.
- `student`: can manage own labs; read-only elsewhere.
- `rookie`: read-only everywhere; cannot create or own labs.

Operational rules:

- New local users default to `rookie` unless an administrator assigns another
  role.
- Only one local-db `assistant` user may exist.
- Keep at least one active local administrator.
- Avoid changing your own role or active state in a way that locks you out.

## Admin Configuration

Administrators can manage shared configuration for hosts, paths and device
catalog metadata from the Admin area.

![Hosts and paths configuration](docs/images/admin-config-hosts-paths.png)

The device catalog controls how the GUI displays device kinds, recognizes
Docker images, chooses icons, maps GUI kinds to Containerlab kinds, injects
defaults and exposes known Web UI metadata.

![Device catalog admin](docs/images/admin-device-catalog.png)

Treat catalog changes as platform changes: validate them with a small lab before
making them broadly available.

## VD Disk Persistence

dNLab can preserve disk state for virtual devices whose images support the
dNLab `/persist` overlay model. Persistent data is stored below the configured
persistence root, normally `/var/lib/docker/dnlab-backups`, using stable
per-device identifiers so renaming a node does not by itself orphan its disk
state.

The default backend is `local-sticky`. It keeps a small placement history and
prefers scheduling a persistent virtual device on the same worker that last ran
it. If the scheduler remaps a stopped persistent device, dNLab can migrate the
overlay before deploy.

The Admin hosts/paths configuration exposes persistence settings:

- `backend`: `local-sticky` or `cephfs`;
- `root`: host path used for persistent VD data;
- `migration fallback`: whether dNLab may fall back to local-sticky handling if
  a shared backend preflight fails;
- `CephFS mountpoint`, `CephFS fstype` and shared marker settings.

CephFS-backed persistence is experimental and has not been production-tested.
Do not rely on it for important labs until you have validated mount behavior,
shared marker checks, failure handling, performance and recovery in your own
environment. Keep `local-sticky` as the default operational choice.

## RealNet BGP

RealNet models connectivity from labs to external networks. NAT mode is simple
egress; BGP mode integrates with administrator-managed route reflector
configuration.

![RealNet BGP admin](docs/images/admin-realnet-bgp.png)

These global settings also back the user-facing RealNet BGP lab-to-lab
communication feature. Users can select allowed peer labs from the RealNet node
properties, subject to RBAC, but the route-reflector parameters are configured
centrally here by administrators.

Configure the route-reflector AS and address (`RR AS`, `RR IP`), the host-side
network used for RealNet infrastructure (`Host network`), the pools assigned to
lab routers (`Router AS pool`, `Router IP pool`), the RealNet node network pool,
the route-reflector image and the shared `RR BGP password`. Keep these ranges
large enough for the expected number of RealNet-connected labs and avoid
overlap with lab, management and physical network prefixes.

Use the Admin page to update global RealNet BGP settings, regenerate the route
reflector password when needed and reconcile the route reflector service.
Device-side BGP configuration remains explicit inside each virtual device.

The global `dnlab-realnet-rr` container is BGP-only infrastructure. It is not
created for NAT-only RealNet labs; those labs only create their per-lab
`dnlab-<lab>-<realnet>-realnet` router.

In the Docker distribution, `/etc/dnlab` is mounted read-only inside the GUI and
multinode containers. Prepare or update the host-side `hosts.yml` before
enabling BGP mode on a RealNet node, or use the Admin write action against a
writable host config path. A minimal BGP block looks like this:

```yaml
infrastructure:
  realnet:
    rr_as: 64512
    rr_ip: 10.0.0.10
    host_net: 10.0.0.0/24
    router_as_pool: 64513-65534
    router_ip_pool: 10.0.0.20-10.0.0.250
    realnet_network_pool: 100.64.0.0/10
    rr_password: change-me
```

## Image Build And Image Sync

`dnlab-image-build` provides an internal API for upload, build jobs and job log
streaming. Build metadata and logs are stored under
`${DNLAB_IMAGE_BUILD_WORKSPACE:-/var/lib/dnlab-image-build}`.
Build contexts are read from `${DNLAB_VRNETLAB_DIR:-/opt/vrnetlab}`, which
must be the `dnlab` branch of `https://github.com/scaci/vrnetlab.git`.

![Image build admin](docs/images/admin-image-build.png)

`dnlab-image-sync` tracks image availability across nodes. After adding or
building images, verify image discovery and image sync before asking users to
start labs that depend on those images.

## Lab Cleanup Reconciler

`dnlab-lab-cleanup` periodically reconciles stale lab artifacts. During first
rollout, keep cleanup in dry-run mode in `/etc/dnlab/hosts.yml`:

```yaml
lab_cleanup:
  enabled: true
  interval_seconds: 300
  grace_seconds: 600
  dry_run: true
```

After validating reports, switch `dry_run` to `false` when the environment is
ready for automatic cleanup.

Manual checks:

```bash
docker compose -f compose.yml exec dnlab-lab-cleanup \
  dnlab-lab-cleanup sync --dry-run --json
```

## Validation

Run `./smoke.sh` after startup or Docker distribution changes. It checks proxy
reachability, GUI isolation, internal API boundaries, image discovery, lab
cleanup state and key Docker-stack invariants.

Run `./preflight.sh` for a fresh-install validation in an isolated Compose
project with an empty database, first-admin bootstrap and login through the
proxy.

## Upgrade

Before upgrading:

1. Back up the auth DB with `pg_dump`.
2. Build or pull the target images.
3. Recreate the internal services and proxy through the Compose dependency
   chain.
4. Run `./smoke.sh`.
5. Inspect cleanup dry-run reports before enabling cleanup execution.

Use [OPERATIONS.md](OPERATIONS.md) for exact commands.

## Backup And Restore

Use `pg_dump` and `psql` for the auth database. Keep dumps outside images and
outside git. The local `auth-db-dumps/` directory is ignored for operator
artifacts.

Restore is an operator action, not a Docker build step. Stop the GUI before
restoring, reset the target schema, load the dump, restart through the proxy and
run smoke checks.

## Troubleshooting

- Login fails: check auth backend configuration, account active state and
  database migrations.
- Admin page is hidden: confirm the user has role `admin`.
- A user cannot create labs: confirm the role is not `rookie`.
- A second assistant cannot be created: dNLab allows only one local-db
  `assistant`.
- Device images are missing: check Docker image discovery, image-build jobs and
  image sync.
- Lab start fails: inspect the pre-deploy plan, service health and
  `dnlab-multinode` logs.
- Device Web UI fails: check lab deployment state, device state, wildcard DNS,
  TLS certificate coverage and proxy configuration.
- Console or logs are empty: confirm the device is running and allow enough
  boot time for virtual network appliances.
