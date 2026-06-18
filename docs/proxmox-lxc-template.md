# dNLab Proxmox LXC Template

This guide is for administrators who want to run dNLab from the ready-made
Proxmox LXC template published on GitHub Container Registry.

This artifact is built and supported for Proxmox LXC. Generic LXC runtimes are
not a supported target for this template.

You do not need to build the template yourself for a normal installation. The
build scripts in `lxc/` are the public reproducible sources for the template and
are mainly useful for release maintainers and auditors.

## Download From GHCR

The primary distribution channel for the LXC template is GHCR:

```text
ghcr.io/scaci/dnlab-lxc-proxmox:0.1.0
```

Install `oras` on your workstation or Proxmox host, authenticate if required,
then pull the artifact:

```bash
mkdir -p dnlab-lxc-0.1.0
cd dnlab-lxc-0.1.0
oras pull ghcr.io/scaci/dnlab-lxc-proxmox:0.1.0
sha256sum -c SHA256SUMS
```

The artifact contains:

```text
dnlab-lxc-proxmox-0.1.0-amd64.tar.zst
LXC-RELEASE-NOTES-0.1.0.md
proxmox-dnlab-ct.conf
apply-proxmox-ct-tuning.sh
SHA256SUMS
```

The same files may also be attached to the GitHub Release as a browser-friendly
mirror, but GHCR is the canonical registry location.

The template does not contain production secrets, admin bootstrap credentials,
site TLS certificates or a fixed hostname. Those are generated or configured
inside each CT.

## Prepare The Proxmox Host

dNLab LXC deployments need enough loop devices for nested lab workloads. On the
Proxmox host, add `loop.max_loop=64` to the kernel command line. With the
default GRUB flow, edit `/etc/default/grub` and include it in
`GRUB_CMDLINE_LINUX_DEFAULT`, for example:

```text
GRUB_CMDLINE_LINUX_DEFAULT="quiet loop.max_loop=64"
```

Then update GRUB and reboot the Proxmox node:

```bash
update-grub
reboot
```

After reboot, verify the host command line:

```bash
grep -w loop.max_loop=64 /proc/cmdline
```

## Create The CT

dNLab needs Docker, Containerlab, nested containers and host-like networking.
Bare metal remains the reference deployment model; Proxmox LXC is usable only
when the CT exposes the required privileges and kernel features.

Copy `dnlab-lxc-proxmox-0.1.0-amd64.tar.zst` to a Proxmox storage that accepts
CT templates, normally:

```bash
cp dnlab-lxc-proxmox-0.1.0-amd64.tar.zst /var/lib/vz/template/cache/
```

Then create a CT from it.

Recommended starting point:

```text
ostype: debian
arch: amd64
cores: 4
memory: 8192
swap: 2048
rootfs: local-lvm:64
net0: name=eth0,bridge=vmbr0,ip=dhcp
features: nesting=1,keyctl=1
unprivileged: 0
```

Use reliable local ext4 or xfs-backed storage for Docker data. Increase CPU,
memory and disk for larger network labs or image-build workloads.

### Proxmox CT Tuning

Before first boot, add the dNLab raw-device tuning to the Proxmox CT config on
the host, normally `/etc/pve/lxc/<CTID>.conf`. The pulled artifact includes
`proxmox-dnlab-ct.conf` and an idempotent helper:

```bash
chmod +x apply-proxmox-ct-tuning.sh
./apply-proxmox-ct-tuning.sh <CTID>
```

The helper creates a timestamped backup of the CT config and appends only
missing lines. If you prefer to edit the CT config manually, add this block:

```text
lxc.cgroup2.devices.allow: c 10:232 rwm
lxc.mount.entry: /dev/kvm dev/kvm none bind,optional,create=file

lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,optional,create=file

lxc.cgroup2.devices.allow: b 7:* rwm
lxc.mount.entry: /dev/loop-control dev/loop-control none bind,optional,create=file

lxc.cgroup2.devices.allow: c 10:229 rwm
lxc.mount.entry: /dev/fuse dev/fuse none bind,optional,create=file

lxc.autodev: 1
```

Restart the CT after changing `/etc/pve/lxc/<CTID>.conf`.

## First Boot

Start the CT and wait for `dnlab-firstboot.service` to finish:

```bash
systemctl status dnlab-firstboot
```

The first-boot service runs `/usr/local/sbin/dnlab-firstboot`, which:

- creates `/opt/dnlab/.env` from `.env.example`;
- generates `POSTGRES_PASSWORD`;
- creates a local self-signed certificate under `/etc/ssl/dnlab`;
- writes `/etc/dnlab/paths.yml`;
- creates a local root SSH key and authorizes it for root-to-root access;
- creates the GUI-to-jumphost SSH key used for Web UI, console and log
  tunnels;
- creates the Compose network, discovers its gateway and writes
  `/etc/dnlab/hosts.yml`;
- starts `docker compose -f compose.yml up -d dnlab-proxy`.

Docker Compose pulls the images needed by the startup path. To preload runtime
helper images used later by lab orchestration, run this optional command inside
the CT:

```bash
cd /opt/dnlab
docker compose -f compose.yml --profile release-images pull
```

The generated HTTPS URL defaults to port `8443`:

```text
https://<ct-ip>:8443/
```

Because the default certificate is self-signed, your browser will show a
certificate warning. Replace it with a site certificate before production use.

## Configure Site Settings

The LXC template is already installed, but the generated instance still needs
site-specific settings in `/opt/dnlab/.env`. This corresponds to the bare metal
environment-file step in [ADMIN_GUIDE.md](../ADMIN_GUIDE.md), with the host
prerequisites and first config files already prepared by first boot.

Run the guided configurator inside the CT:

```bash
sudo dnlab-configure-env
```

It prompts for:

- `DNLAB_PROXY_SERVER_NAME`: public GUI hostname or IP;
- `DNLAB_PROXY_HTTP_PORT` and `DNLAB_PROXY_HTTPS_PORT`: public proxy ports;
- `DNLAB_PROXY_TLS_DIR`: host directory mounted into the proxy as
  `/etc/ssl/dnlab`;
- `DNLABGUI_ALLOWED_ORIGINS`: browser-facing HTTPS origin;
- `DNLAB_PROXY_CERT_FILE` and `DNLAB_PROXY_CERT_KEY_FILE`: certificate and key
  paths as seen inside the proxy container.

If the configured certificate or key is missing under the TLS directory, the
script can generate a new local self-signed certificate for the selected
hostname. For production, install a publicly trusted certificate and point the
certificate settings at that material instead.

The generated `/etc/dnlab/paths.yml` should match the bare metal shape and
include both SSH keys:

```yaml
ssh_key: /root/.ssh/id_ed25519_github_dnlab
ssh_gui_key: /root/.ssh/dnlab-gui.key
```

If you edit `.env` manually instead of using the guided script, recreate the GUI
and proxy after changing hostname, origin, ports or TLS settings:

```bash
cd /opt/dnlab
docker compose -f compose.yml up -d --force-recreate dnlab-gui dnlab-proxy
```

## Create The First Admin

The template intentionally does not store an admin password. After first boot,
run this inside the CT:

```bash
cd /opt/dnlab
DNLABGUI_BOOTSTRAP_ADMIN_USERNAME=admin \
DNLABGUI_BOOTSTRAP_ADMIN_PASSWORD='<one-time-password>' \
docker compose -f compose.yml --profile seed-admin run --rm dnlab-auth-seed
```

Then open:

```text
https://<ct-ip>:8443/
```

## Validate

Useful checks inside the CT:

```bash
docker version
docker compose version
containerlab version
docker compose -f /opt/dnlab/compose.yml ps
```

Run the HTTPS smoke check:

```bash
cd /opt/dnlab
COMPOSE_FILES=compose.yml \
DNLAB_SMOKE_PROXY_URL=https://<ct-ip>:8443/ \
DNLAB_SMOKE_CURL_INSECURE=1 \
./smoke.sh
```

To rerun the first-boot configurator safely:

```bash
sudo dnlab-firstboot
```

Existing `hosts.yml` and `paths.yml` are preserved by default. To regenerate
them:

```bash
sudo DNLAB_FIRSTBOOT_REWRITE_HOSTS=1 DNLAB_FIRSTBOOT_REWRITE_PATHS=1 dnlab-firstboot
```

To rerun only the hostname, port and TLS prompts, use:

```bash
sudo dnlab-configure-env
```

## Maintainers

The template archive is generated by dNLab maintainers and published as an OCI
artifact on GHCR. Users should pull the artifact from GHCR instead of building
it locally.

Build from a clean, tagged distribution checkout on a Linux build host with root
access, network access, `debootstrap`, `tar`, `zstd`, `curl` and `gpg`:

```bash
sudo lxc/build-proxmox-template.sh --version 0.1.0
```

The output files are written to `dist/lxc` by default:

```text
dnlab-lxc-proxmox-0.1.0-amd64.tar.zst
LXC-RELEASE-NOTES-0.1.0.md
proxmox-dnlab-ct.conf
apply-proxmox-ct-tuning.sh
SHA256SUMS
```

Publish the template as an OCI artifact after GHCR image publication,
source-archive publication and release smoke validation have completed:

```bash
cd dist/lxc
oras login ghcr.io
oras push ghcr.io/scaci/dnlab-lxc-proxmox:0.1.0 \
  dnlab-lxc-proxmox-0.1.0-amd64.tar.zst:application/vnd.proxmox.lxc.template.v1.tar+zstd \
  LXC-RELEASE-NOTES-0.1.0.md:text/markdown \
  proxmox-dnlab-ct.conf:text/plain \
  apply-proxmox-ct-tuning.sh:application/x-sh \
  SHA256SUMS:text/plain
```

Use `0.1.0` as the GHCR artifact tag. Keep `v0.1.0` for the GitHub Release
and git source tag only, otherwise GitHub Packages shows a second container
version for the same release. Do not commit the generated template archive to
the repository. If the files are also attached to the GitHub Release as a
mirror, add the template checksum to the release `SHA256SUMS` without dropping
existing source-archive checksum entries.

The public reproducible sources for the LXC template are:

- `lxc/build-proxmox-template.sh`
- `lxc/apply-proxmox-ct-tuning.sh`
- `lxc/dnlab-configure-env.sh`
- `lxc/dnlab-firstboot.sh`
- `lxc/dnlab-firstboot.service`
- `lxc/proxmox-dnlab-ct.conf`
- the tagged dNLab distribution files copied into `/opt/dnlab`

The operational release helpers remain private. Their authoritative copies live
under `/root/dnlab-dev-docs/scripts`; local copies under `/opt/dnlab/scripts`
are ignored and must not be published.
