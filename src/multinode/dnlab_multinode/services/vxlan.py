"""Dataplane VxLAN tunnel management."""

from __future__ import annotations

import logging

from dnlab_multinode.models.schedule import CrossHostLink
from dnlab_multinode.services.ssh import SSHClient

log = logging.getLogger(__name__)


def create_dataplane_tunnel(
    link: CrossHostLink,
    src_client: SSHClient,
    tgt_client: SSHClient,
    src_host_ip: str,
    tgt_host_ip: str,
) -> None:
    """Create VxLAN tunnel for a cross-host dataplane link.

    Uses `containerlab tools vxlan create` on both sides.
    """
    log.info("Creating VxLAN %d: %s (on %s ↔ %s)",
             link.vxlan_id, link, link.source_host, link.target_host)

    # Source side
    src_cmd = (
        f"containerlab tools vxlan create "
        f"--remote {tgt_host_ip} "
        f"--id {link.vxlan_id} "
        f"--link {link.source_host_iface}"
    )
    src_client.run(src_cmd)
    log.debug("[%s] VxLAN source side created: %s", link.source_host, src_cmd)

    # Target side
    tgt_cmd = (
        f"containerlab tools vxlan create "
        f"--remote {src_host_ip} "
        f"--id {link.vxlan_id} "
        f"--link {link.target_host_iface}"
    )
    tgt_client.run(tgt_cmd)
    log.debug("[%s] VxLAN target side created: %s", link.target_host, tgt_cmd)


def destroy_dataplane_tunnel(
    link: CrossHostLink,
    src_client: SSHClient,
    tgt_client: SSHClient,
) -> None:
    """Remove VxLAN tunnel interfaces on both sides.

    ``containerlab tools vxlan create --link <iface>`` creates a sibling
    interface named ``vx-<iface>``. We delete that name directly — it is the
    only reliable target, because ``containerlab tools vxlan delete`` uses a
    prefix filter that would affect other labs.
    """
    log.info("Removing VxLAN %d: %s", link.vxlan_id, link)

    for client, host_iface, host_name in [
        (src_client, link.source_host_iface, link.source_host),
        (tgt_client, link.target_host_iface, link.target_host),
    ]:
        _delete_vxlan_iface(client, host_iface, host_name)


def _delete_vxlan_iface(client: SSHClient, host_iface: str, host_name: str) -> None:
    """Delete the ``vx-<host_iface>`` VxLAN interface on a host (idempotent)."""
    vx_name = f"vx-{host_iface}"
    # Linux caps interface names at 15 chars — containerlab truncates the same way.
    vx_name = vx_name[:15]
    rc, _, err = client.run_no_check(f"ip link delete {vx_name}")
    if rc == 0:
        log.debug("[%s] Deleted VxLAN iface %s", host_name, vx_name)
    else:
        # Not fatal — the iface may already be gone (e.g. after clab destroy
        # removed the dummy it was paired with).
        log.debug("[%s] VxLAN iface %s delete rc=%d err=%s",
                  host_name, vx_name, rc, err.strip())


def verify_tunnels(
    links: list[CrossHostLink],
    ssh_clients: dict[str, SSHClient],
) -> list[dict]:
    """Verify that all VxLAN tunnel interfaces are UP.

    Returns: list of {link, status} dicts
    """
    results = []
    for link in links:
        status = "up"

        for host_name, iface in [
            (link.source_host, link.source_host_iface),
            (link.target_host, link.target_host_iface),
        ]:
            client = ssh_clients.get(host_name)
            if not client:
                status = "error"
                continue

            rc, out, _ = client.run_no_check(
                f"ip link show {iface} 2>/dev/null | head -1"
            )
            if rc != 0 or "UP" not in out.upper():
                status = "down"

        results.append({"link": str(link), "vxlan_id": link.vxlan_id, "status": status})

    return results
