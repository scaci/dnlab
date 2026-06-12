# dNLab

<p align="center">
  <img src="docs/images/lockup-orizzontale-light.svg" alt="dNLab logo" width="360">
</p>

> Build network labs on one node or across many, orchestrated automatically and transparently.

![Build](https://img.shields.io/badge/build-TODO-lightgrey)
![License](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue)

## Overview

**dNLab** (distributed Network Labs) is an application built around a [Containerlab](https://containerlab.dev) core. It lets you build network labs on a single node or automatically distribute them across multiple nodes, so a topology can start small and grow beyond the capacity of one machine.

Orchestration is fully automatic and transparent. You design the topology; dNLab handles placing and managing it across the configured infrastructure — including single-host placement, device scheduling, intra-host links and cross-host dataplane connectivity when multiple workers are present. The distribution layer stays out of your way: you reason about the network you want, not the hosts it runs on.

dNLab is built for learning and experimenting with networking. Labs can be shared and interconnected, and a dedicated Role-Based Access Control (RBAC) system defines roles and permissions to make collaboration between users straightforward.

## Host Requirements

Recommended baseline:

- Debian 13 "trixie" official stable, minimal server install, `amd64`.
- Bare metal host with direct access to physical CPU, memory, storage and
  networking resources.
- One or more bare metal nodes are the reference architecture. A single host is
  a valid deployment model; in that case it acts as both master and worker.
- As an alternative, one or more Proxmox LXC containers may be used when they
  are configured to expose the required host resources and privileges.
- Virtual machines are not a supported reference architecture for dNLab. dNLab
  is effective and efficient only when it can operate close to physical
  resources, especially for nested container, networking and virtual device
  workloads.
- Docker Engine 29.x stable from Docker's official Debian repository.
- Docker Compose plugin installed through the official Docker packages.
- systemd, cgroup v2 and the stock Debian 13 kernel.
- Local Docker storage on a reliable ext4 or xfs filesystem.
- [ContainerLab](https://containerlab.dev) installed.
- Root or sudo access for Docker, ContainerLab and host networking.
- Public inbound access only to the proxy ports, normally 80/443.

Use the Docker packages from Docker's repository, not the generic Debian
`docker.io` package. Record the output of `docker version` and
`docker compose version` before deploying a dNLab stack.

References:

- Debian 13 release notes: <https://www.debian.org/releases/trixie/release-notes/>
- Docker Engine on Debian: <https://docs.docker.com/engine/install/debian/>
- Docker Engine 29 release notes: <https://docs.docker.com/engine/release-notes/29/>

For Proxmox deployments, see
[dNLab Proxmox LXC Template](docs/proxmox-lxc-template.md). Pull the ready-made
LXC template from GitHub Container Registry; it keeps secrets, TLS material and
site-specific host configuration out of the published archive.

### Suggested multinode reference design

<p align="center">
  <img src="infrastructure.svg" alt="Multinode Infrastructure" width="400">
</p>

## Docker Distribution Stack

This repository contains the Docker distribution stack for dNLab. It uses GHCR
image references and documents source availability for published images in
[SOURCE.md](SOURCE.md).

Public release packages are published as `ghcr.io/scaci/dnlab-*` container
images and are linked to this public `scaci/dnlab` repository. The matching
AGPL source archives are attached to the corresponding GitHub Release.

Current release: `0.1.0`. The Compose stack requires `DNLAB_VERSION=0.1.0`
in `.env`; this selects the published GHCR images for the release.

The stack contains:

- `dnlab-proxy`: Apache reverse proxy, exposed on the host.
- `dnlab-gui`: FastAPI GUI, internal only.
- `dnlab-multinode`: internal API for orchestration.
- `dnlab-lab-cleanup`: periodic stale-artifact reconciler, built from a
  dedicated slim image (`Dockerfile.cleanup`) and operating entirely over SSH.
- `dnlab-image-build`: internal API for image-build jobs and log streaming.
- `dnlab-auth-db`: support Postgres service for GUI local-db auth.

## Quick Start

Prepare host directories and configuration:

```bash
sudo apt-get install -y ripgrep
sudo mkdir -p /etc/dnlab /root/dnlab-topologies \
  /var/lib/docker/dnlab-backups /var/log/dnlab-gui \
  /var/log/dnlab-multinode /var/lib/dnlab-image-build /opt/vrnetlab
```

Install the dNLab vrnetlab image-build tree if it is not already present:

```bash
if [ ! -d /opt/vrnetlab/.git ]; then
  sudo git clone --branch dnlab https://github.com/scaci/vrnetlab.git /opt/vrnetlab
else
  git -C /opt/vrnetlab remote -v
  git -C /opt/vrnetlab branch --show-current
fi
```

Create `/etc/dnlab/hosts.yml` and `/etc/dnlab/paths.yml` for your site before
deploying real labs. The GUI and the internal services mount `/etc/dnlab`
read-only.

For a single-node install, configure `hosts.yml` with the Docker-network
gateway address that containers can use to reach the host SSH daemon, and no
remote workers. Do not use `localhost` for `master.host`: inside the dNLab
containers it refers to the container itself, not to the Docker host. For
example, if the `dnlab_dnlab-internal` gateway is `172.18.0.1`:

```yaml
infrastructure:
  master:
    host: 172.18.0.1
    ssh_user: root
  workers: {}
```

The gateway address is installation-specific; confirm it with Docker before
writing `hosts.yml`, for example:

```bash
docker network inspect dnlab_dnlab-internal --format '{{(index .IPAM.Config 0).Gateway}}'
```

Use top-level keys in `/etc/dnlab/paths.yml`. Do not wrap these values in
`paths:` or `persistence:` sections:

```yaml
hosts_file: /etc/dnlab/hosts.yml
image_sync_state: /var/lib/dnlab-image-sync/state.json
persist_root: /var/lib/docker/dnlab-backups
topologies_dir: /root/dnlab-topologies
ssh_key: /root/.ssh/id_ed25519_github_dnlab
log_dir_multinode: /var/log/dnlab-multinode
log_dir_gui: /var/log/dnlab-gui
tmp_dir: /tmp
containerlab_bin: /usr/bin/containerlab
docker_socket: unix:///var/run/docker.sock
```

Multi-node sites can add workers later without changing the Compose entrypoint.

For a multi-node install, reserve a dedicated network for cross-host lab
dataplane traffic whenever possible. Declare the selected interface alias in
`/etc/dnlab/hosts.yml` for the master and for every worker so orchestration uses
the intended fabric instead of an incidental management interface.

Before installation, configure SSH key-based access from the master to every
host listed in `hosts.yml`. This includes passwordless SSH from the master to
itself, using the same master host value declared in the inventory. For
manual installs, create the dedicated keypair on the master and keep the
private key there:

```bash
sudo install -d -m 0700 /root/.ssh
sudo test -f /root/.ssh/id_ed25519_github_dnlab || \
  sudo ssh-keygen -t ed25519 -N '' \
    -f /root/.ssh/id_ed25519_github_dnlab \
    -C "dnlab@$(hostname)"
sudo chmod 0600 /root/.ssh/id_ed25519_github_dnlab
```

Install `/root/.ssh/id_ed25519_github_dnlab.pub` in
`/root/.ssh/authorized_keys` on the configured master target and on every
worker. For remote workers, use:

```bash
sudo ssh-copy-id -i /root/.ssh/id_ed25519_github_dnlab.pub root@<worker-host>
```

For single-node installs, pre-populate `/root/.ssh/known_hosts` with the Docker
gateway host key. For multi-node installs, record the configured master and
worker host keys there as well before starting the containers, because
`/root/.ssh` is mounted read-only inside them. Validate the result from the
master with `ssh -o BatchMode=yes root@<host> true`.

Create or install a TLS certificate before starting the browser-facing proxy.
dNLab sets the GUI session cookie as HTTPS-only, so even local test installs
should use TLS. For a local self-signed test certificate:

```bash
sudo mkdir -p /etc/ssl/dnlab
sudo openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout /etc/ssl/dnlab/dnlab-gui.key \
  -out /etc/ssl/dnlab/dnlab-gui.crt \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
```

Start the stack:

```bash
cp .env.example .env
# Edit .env and set POSTGRES_PASSWORD plus the TLS values below.
docker compose -f compose.yml -f compose.tls.yml --profile release-images pull
docker compose -f compose.yml -f compose.tls.yml up -d dnlab-proxy
```

The release image settings copied from `.env.example` are:

```env
DNLAB_VERSION=0.1.0
DNLAB_IMAGE_PREFIX=ghcr.io/scaci/
DNLAB_RUNTIME_IMAGE_PREFIX=ghcr.io/scaci/dnlab-
```

The public release images are readable without a registry login. If you point
the image prefixes at a private mirror, authenticate Docker separately from Git
SSH access before pulling images:

```bash
echo '<github-token-with-package-read-access>' | docker login ghcr.io -u <github-user> --password-stdin
```

SSH access to the GitHub repository does not grant Docker access to private
container registries.

For a local self-signed HTTPS test, use:

```env
DNLAB_PROXY_SERVER_NAME=localhost
DNLAB_PROXY_WEBUI_SUFFIX=localhost
DNLAB_PROXY_HTTPS_PORT=8443
DNLAB_PROXY_TLS_DIR=/etc/ssl/dnlab
DNLABGUI_ALLOWED_ORIGINS=https://localhost:8443
DNLABGUI_WEBUI_HOST_SUFFIX=localhost
```

With those values, the `release-images` pull profile downloads the full
published `ghcr.io/scaci/dnlab-*:0.1.0` image set, including runtime images
that are created later by lab orchestration. Use `--profile release-images`
only for `pull`; normal `up` commands should continue to target `dnlab-proxy`
or explicit runtime services.

Run the initial smoke check:

```bash
COMPOSE_FILES=compose.yml:compose.tls.yml \
DNLAB_SMOKE_PROXY_URL=https://localhost:8443/ \
DNLAB_SMOKE_CURL_INSECURE=1 \
./smoke.sh
```

The default local test URL with the values above is
`https://localhost:8443/`. Browsers will warn about a self-signed certificate;
accept it only for local testing. Production deployments should use a publicly
trusted certificate and normally expose HTTPS on port 443.

In this distribution stack the GUI, multinode and lab-cleanup services default
to the GHCR images selected by `DNLAB_IMAGE_PREFIX` and `DNLAB_VERSION`.

## Documentation

- [USER_GUIDE.md](USER_GUIDE.md): browser workflows for lab users.
- [ADMIN_GUIDE.md](ADMIN_GUIDE.md): platform administration guide.
- [OPERATIONS.md](OPERATIONS.md): production-oriented runbook covering fresh
  install, TLS mode, production hardening, upgrade, backups and smoke checks.
- [CONTRIBUTING.md](CONTRIBUTING.md): contribution process and DCO
  requirements.
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md): third-party notices and
  redistribution notes.
- [DCO](DCO): Developer Certificate of Origin text.
- [LICENSE](LICENSE): AGPL-3.0-or-later license text.
- [LICENSE_FAQ.md](LICENSE_FAQ.md): practical notes for on-premises use,
  AGPL obligations and commercial licensing.
- [SOURCE.md](SOURCE.md): source availability policy for published dNLab
  container images.

## Docker Auth Database

The DB image is the stock `postgres:16-alpine` image. It must not contain a
production DB dump. A fresh install starts with an empty named volume; on GUI
startup the compose command runs `alembic upgrade head` against that empty DB.

To seed the first local-db admin user:

```bash
DNLABGUI_BOOTSTRAP_ADMIN_USERNAME=admin \
DNLABGUI_BOOTSTRAP_ADMIN_PASSWORD='<one-time-password>' \
docker compose -f compose.yml --profile seed-admin run --rm dnlab-auth-seed
```

Migrating an existing auth DB is an operator procedure for a specific
environment, not a build step. First dump the old DB and keep a backup of the
target DB:

```bash
mkdir -p auth-db-dumps
docker exec <old-postgres-container> sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' \
  > auth-db-dumps/dnlab_auth_from_old_container.sql

docker compose -f compose.yml exec -T dnlab-auth-db sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' \
  > auth-db-dumps/dnlab_auth_compose_before_restore.sql
```

Then stop the GUI and restore the dump:

```bash
docker compose -f compose.yml stop dnlab-gui
docker compose -f compose.yml exec -T dnlab-auth-db sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "drop schema public cascade; create schema public;"'
docker compose -f compose.yml exec -T dnlab-auth-db sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' \
  < auth-db-dumps/dnlab_auth_from_old_container.sql
docker compose -f compose.yml -f compose.tls.yml up -d dnlab-proxy
```

## Docker Service Boundaries

The compose file intentionally exposes only `dnlab-proxy`. The GUI talks to
`dnlab-multinode` through `DNLAB_MULTINODE_API_URL`; local Python fallback stays
available in the codebase but is not the target path for this stack. The Docker
GUI image does not install the `dnlab-multinode` Python package; if a GUI path
falls back to local orchestrator imports inside this stack, treat it as a
regression.

Local GUI fallbacks are kept only for standalone development outside Docker.
Inside this stack, `DNLAB_MULTINODE_API_URL` and `DNLAB_IMAGE_BUILD_API_URL` are
mandatory service boundaries.

The Docker network is project-scoped by Compose. This keeps ad-hoc fresh-install
checks with `docker compose -p <name>` isolated from the main stack.

The GUI container does not mount `/var/run/docker.sock`. Docker image discovery
is routed through `dnlab-multinode`; image-build operations are routed through
`dnlab-image-build`.

## Docker Validation

Fresh-install smoke check:

```bash
mkdir -p /tmp/dnlab-fresh-topologies /tmp/dnlab-fresh-tls
openssl req -x509 -nodes -newkey rsa:2048 -days 7 \
  -keyout /tmp/dnlab-fresh-tls/dnlab-gui.key \
  -out /tmp/dnlab-fresh-tls/dnlab-gui.crt \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

POSTGRES_PASSWORD=fresh-check-password \
DNLAB_PROXY_HTTP_PORT=18088 \
DNLAB_PROXY_HTTPS_PORT=18443 \
DNLAB_PROXY_SERVER_NAME=localhost \
DNLAB_PROXY_WEBUI_SUFFIX=localhost \
DNLAB_PROXY_TLS_DIR=/tmp/dnlab-fresh-tls \
DNLABGUI_ALLOWED_ORIGINS=https://localhost:18443 \
DNLABGUI_WEBUI_HOST_SUFFIX=localhost \
DNLAB_TOPOLOGIES_DIR=/tmp/dnlab-fresh-topologies \
docker compose -p dnlabfresh -f compose.yml -f compose.tls.yml up -d dnlab-proxy

POSTGRES_PASSWORD=fresh-check-password \
DNLAB_PROXY_HTTP_PORT=18088 \
DNLAB_PROXY_HTTPS_PORT=18443 \
DNLAB_PROXY_SERVER_NAME=localhost \
DNLAB_PROXY_WEBUI_SUFFIX=localhost \
DNLAB_PROXY_TLS_DIR=/tmp/dnlab-fresh-tls \
DNLABGUI_ALLOWED_ORIGINS=https://localhost:18443 \
DNLABGUI_WEBUI_HOST_SUFFIX=localhost \
DNLAB_TOPOLOGIES_DIR=/tmp/dnlab-fresh-topologies \
DNLABGUI_BOOTSTRAP_ADMIN_USERNAME=freshadmin \
DNLABGUI_BOOTSTRAP_ADMIN_PASSWORD='<freshadmin-password>' \
docker compose -p dnlabfresh -f compose.yml --profile seed-admin run --rm dnlab-auth-seed

curl -ki https://localhost:18443/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"freshadmin","password":"<freshadmin-password>"}'

docker compose -p dnlabfresh -f compose.yml down -v
```

Run `./smoke.sh` after startup or distribution changes. For self-signed TLS,
set `DNLAB_SMOKE_PROXY_URL` and `DNLAB_SMOKE_CURL_INSECURE=1`. The smoke check
verifies that the proxy is reachable, the GUI image does not install or import
`dnlab-multinode`, the GUI does not mount the Docker socket, RealNet RR status
goes through the API, `hosts.yml` validation is served by `dnlab-multinode`,
`dnlab_frr` resolves to ContainerLab kind `linux`, and the lab-cleanup daemon
has published state.

`dnlab-realnet-rr` is only created when RealNet BGP is enabled and
`infrastructure.realnet.rr_ip` plus `infrastructure.realnet.host_net` are
configured in `hosts.yml`. NAT-only RealNet labs create their per-lab
`dnlab-<lab>-<realnet>-realnet` router, but do not create the global route
reflector container.

Run `./preflight.sh` to exercise a fresh install in an isolated Compose project
with an empty DB, first-admin bootstrap and login through the proxy.

## Docker Operations

The lab cleanup reconciler runs as its own Compose service and writes state to
`${DNLAB_LAB_CLEANUP_STATE_DIR:-/var/lib/dnlab-lab-cleanup}`. During first
rollout, set `lab_cleanup.dry_run: true` in `/etc/dnlab/hosts.yml`, inspect
the report, then switch it to `false` when ready:

```bash
docker compose -f compose.yml exec dnlab-lab-cleanup \
  dnlab-lab-cleanup sync --dry-run --json

docker compose -f compose.yml exec dnlab-lab-cleanup \
  dnlab-lab-cleanup sync --execute --json
```

The image-build service exposes an internal API at `http://dnlab-image-build:8082`.
Job metadata and logs are stored under
`${DNLAB_IMAGE_BUILD_WORKSPACE:-/var/lib/dnlab-image-build}` so they survive a
service restart. Jobs that were `queued` or `running` during a restart are
reloaded as failed with an interruption log line.

TLS proxy profile:

```bash
DNLAB_PROXY_SERVER_NAME=dnlab.example.com \
DNLAB_PROXY_WEBUI_SUFFIX=dnlab.example.com \
DNLABGUI_ALLOWED_ORIGINS=https://dnlab.example.com \
DNLABGUI_WEBUI_HOST_SUFFIX=dnlab.example.com \
DNLAB_PROXY_TLS_DIR=/etc/ssl/dnlab \
docker compose -f compose.yml -f compose.tls.yml up -d --force-recreate dnlab-gui dnlab-proxy
```

The TLS directory must contain the certificate and key referenced by
`DNLAB_PROXY_CERT_FILE` and `DNLAB_PROXY_CERT_KEY_FILE`, defaulting to
`/etc/ssl/dnlab/dnlab-gui.crt` and `/etc/ssl/dnlab/dnlab-gui.key` inside the
proxy container. Wildcard WebUI needs DNS and certificate coverage for both the
GUI hostname and `*.${DNLAB_PROXY_WEBUI_SUFFIX}`. The `compose.tls.yml`
override requires `DNLABGUI_ALLOWED_ORIGINS` and `DNLABGUI_WEBUI_HOST_SUFFIX`
so WebSocket origin checks and wildcard WebUI URLs match the browser-facing
hostname.

Production hardening profile:

```bash
DNLAB_PROXY_SERVER_NAME=dnlab.example.com \
DNLAB_PROXY_WEBUI_SUFFIX=dnlab.example.com \
DNLABGUI_ALLOWED_ORIGINS=https://dnlab.example.com \
DNLABGUI_WEBUI_HOST_SUFFIX=dnlab.example.com \
DNLAB_PROXY_TLS_DIR=/etc/ssl/dnlab \
docker compose -f compose.yml -f compose.tls.yml -f compose.hardened.yml up -d --force-recreate dnlab-gui dnlab-proxy
```

The hardening override makes the GUI root filesystem read-only, drops GUI Linux
capabilities, adds tmpfs for transient paths and applies `no-new-privileges` to
GUI, proxy, auth DB and image-build. `dnlab-multinode` remains the privileged
orchestration boundary; `dnlab-image-build` keeps the Docker socket because
image builds require it.

## Key Features

- **Containerlab-based core** — leverages Containerlab for defining and running virtual network topologies.
- **Single-node or multi-node operation** — a lab can run on one host or be
  spread automatically across multiple worker nodes.
- **Automatic, transparent orchestration** — placement, scheduling, and link management are handled for you; no manual host assignment required.
- **Lab sharing and interconnection** — share labs with other users and connect labs together.
- **RBAC-based collaboration** — built-in roles and permissions simplify teamwork on shared labs.
- **Persistent VD disks** — supported virtual devices can keep disk state under
  the configured persistence root and reuse it across redeploys.

CephFS-backed persistence is an experimental option and has not been validated
for production use. Treat it as a lab feature until it is explicitly tested in
your environment.

## Architecture

A user submits a topology to the dNLab orchestrator. The orchestrator schedules
devices on the configured worker capacity, wiring same-host links locally and,
when multiple workers are present, stitching cross-host links over the
dataplane. The following diagram shows a multi-node deployment.

```mermaid
flowchart TD
    U[User / Topology definition] --> O[dNLab Orchestrator]
    O --> N1[Worker Node 1]
    O --> N2[Worker Node 2]
    O --> N3[Worker Node 3]
    N1 <-->|cross-host dataplane| N2
    N2 <-->|cross-host dataplane| N3
```

## Use Cases

- **Self-study** — students build routing and switching labs that outgrow a single laptop or server.
- **Automation development** — engineers test network automation tooling against realistic, multi-device topologies.
- **Team labs** — a group shares a pool of nodes and collaborates on labs with scoped access via RBAC.
- **Interconnected scenarios** — separate labs are linked together to model larger, multi-domain networks.

## Getting Started

### Prerequisites

- A Containerlab-compatible environment on each node.
- Container runtime and the virtual network device images you intend to use.
- The dNLab vrnetlab image-build tree cloned at `/opt/vrnetlab` from
  `https://github.com/scaci/vrnetlab.git`, branch `dnlab`.
- One or more Linux hosts to run lab devices. In a single-node deployment, the
  master host is also the worker.

### Installation

```bash
git clone https://github.com/scaci/dnlab.git
cd dnlab
cp .env.example .env
# Edit .env, set POSTGRES_PASSWORD and TLS values.
docker compose -f compose.yml -f compose.tls.yml --profile release-images pull
docker compose -f compose.yml -f compose.tls.yml up -d dnlab-proxy
```

The `release-images` profile is a pull-only helper. It downloads both Compose
service images and runtime helper images used later by lab orchestration; do
not use it with normal `up` commands.

### Quick Start

```bash
COMPOSE_FILES=compose.yml:compose.tls.yml \
DNLAB_SMOKE_PROXY_URL=https://localhost:8443/ \
DNLAB_SMOKE_CURL_INSECURE=1 \
./smoke.sh
```

## Usage

Open the GUI through the HTTPS proxy, seed the first administrator if this is a
fresh install, and create or import labs from the browser. The backend uses the
images selected by `DNLAB_VERSION`, `DNLAB_IMAGE_PREFIX` and
`DNLAB_RUNTIME_IMAGE_PREFIX`; no local dNLab image build is part of the public
installation flow.

## Collaboration & RBAC

dNLab includes a Role-Based Access Control system that governs who can view, modify, and run shared labs. Permissions are attached to roles, and roles are assigned to users, so a team can grant the right level of access — for example, read-only access to a shared lab versus full control — without managing permissions one user at a time.

## Contributing

Contributions are welcome. Please open an issue to discuss significant changes
before submitting a pull request.

All contributions must be submitted under `AGPL-3.0-or-later` and certified
with the Developer Certificate of Origin 1.1. See
[CONTRIBUTING.md](CONTRIBUTING.md) for details.

## License

dNLab is licensed under the GNU Affero General Public License v3.0 or later
(`AGPL-3.0-or-later`). See [LICENSE](LICENSE) for details.

Internal on-premises use does not require publishing your modifications solely
because of that internal use. See [LICENSE_FAQ.md](LICENSE_FAQ.md) for practical
notes on AGPL obligations and commercial licensing. See [SOURCE.md](SOURCE.md)
for the source availability policy for published dNLab container images.
