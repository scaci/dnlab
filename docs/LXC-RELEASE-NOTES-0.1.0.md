# dNLab Proxmox LXC template 0.1.0

- GitHub Release: https://github.com/scaci/dnlab/releases/tag/v0.1.0
- Primary registry: ghcr.io/scaci/dnlab-lxc-proxmox:0.1.0
- Debian suite: trixie
- Architecture: amd64
- dNLab version: 0.1.0
- Template asset: dnlab-lxc-proxmox-0.1.0-amd64.tar.zst
- Guided configurator: /usr/local/sbin/dnlab-configure-env

## Assets

- dnlab-lxc-proxmox-0.1.0-amd64.tar.zst
- LXC-RELEASE-NOTES-0.1.0.md
- SHA256SUMS with the published asset checksums included
- proxmox-dnlab-ct.conf
- apply-proxmox-ct-tuning.sh
- prepare-proxmox-ct.sh

## Proxmox Requirements

This template is built for Proxmox LXC. Generic LXC is not a supported target
for this distribution artifact.

Create the CT so it exposes the privileges needed by Docker, Containerlab and
host-like networking. Recommended baseline:

- features: nesting=1,keyctl=1
- unprivileged: 1
- cores: 4
- memory: 8192
- rootfs: 64G or larger on reliable local storage

The Proxmox host should boot with loop.max_loop=64. Add it to the host kernel
command line, run update-grub and reboot the Proxmox node.

Prepare the CT config before first boot:

  ./prepare-proxmox-ct.sh <CTID>

The helper validates the Proxmox-side settings, applies the dNLab raw-device
tuning and creates a timestamped backup of the CT config.

## First Boot

Import the template into Proxmox, create the CT, start it, then wait for
dnlab-firstboot.service. First boot prepares local configuration, generates TLS
and SSH key material, starts the stack, preloads release images best-effort and
creates the first administrator credentials in /root/dnlab-first-admin.txt.
Run dnlab-configure-env inside the CT to adjust the public hostname, ports and
TLS certificate paths, then recreate dnlab-gui and dnlab-proxy when prompted.
Open https://<ct-ip>:8443/ and sign in with the generated credentials:

  cat /root/dnlab-first-admin.txt

## Security Notes

The template does not include production secrets, admin bootstrap credentials,
site TLS certificates or a fixed hostname. First boot generates instance-local
configuration.

If this release already has a SHA256SUMS asset from the source-archive
publication step, merge the template checksum into that file instead of
replacing the source-archive checksum entries.

The Compose release-images profile can be rerun manually if the best-effort
first boot preload hit a temporary network or registry error:

  cd /opt/dnlab
  docker compose -f compose.yml --profile release-images pull

See /opt/dnlab/docs/PROXMOX_LXC_TEMPLATE.md inside the template for the full
guide.
