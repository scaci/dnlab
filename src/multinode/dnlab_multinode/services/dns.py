"""Centralized DNS container management (dnsmasq)."""

from __future__ import annotations

import logging

from dnlab_multinode.models.topology import DistributedTopology
from dnlab_multinode.services.hostsfile import (
    HostEntry, collect_hosts_entries, get_upstream_dns, render_hosts_file,
)
from dnlab_multinode.services.images import image_for
from dnlab_multinode.services.jumphost import (
    allocate_jumphost_ip,
    ensure_jumphost_network,
)
from dnlab_multinode.services.mgmt_ips import ipv4_reservations
from dnlab_multinode.services.paths import PATHS
from dnlab_multinode.services.ssh import SSHClient

log = logging.getLogger(__name__)


def dns_container_name(lab_name: str) -> str:
    return f"dnlab-{lab_name}-dns"


def hosts_file_path(lab_name: str) -> str:
    """Path on the master where the merged hosts file lives."""
    return f"{PATHS.tmp_dir}/dnlab-{lab_name}-dns-hosts"


def compute_dns_mgmt_ip(subnet: str) -> str:
    """Penultimate reserved IP in the mgmt subnet."""
    return ipv4_reservations(subnet).dns


def deploy_dns(
    topo: DistributedTopology,
    master_client: SSHClient,
    clients: dict[str, SSHClient],
    extra_entries: list[HostEntry] | None = None,
) -> tuple[str, str, list[str], int]:
    """Deploy the DNS container on the master.

    Returns: (container_name, resolver_ip, upstream_servers, entry_count)
    """
    container = dns_container_name(topo.name)
    image = image_for("dns")
    network = topo.jumphost_net.network
    host_file = hosts_file_path(topo.name)

    log.info("Deploying centralized DNS: %s", container)

    # 0. Pre-flight: image must exist locally. Release installs preload these
    #    images with the Compose `release-images` pull profile.
    rc, _, _ = master_client.run_no_check(
        f"docker image inspect {image} >/dev/null 2>&1"
    )
    if rc != 0:
        raise RuntimeError(
            f"DNS image '{image}' not found on master. "
            "Run: docker compose --profile release-images pull"
        )

    # 1. Collect /etc/hosts blocks from every node
    entries = collect_hosts_entries(topo.name, clients)
    if extra_entries:
        entries = _merge_entries(entries, extra_entries)
    log.info("Collected %d unique DNS entries from %d hosts",
             len(entries), len(clients))

    # 2. Discover upstream resolvers from the master
    upstream = get_upstream_dns(master_client)

    # 3. Upload merged hosts file to the master
    content = render_hosts_file(entries)
    master_client.upload_text(content, host_file)
    log.debug("Uploaded hosts file to %s (%d bytes)", host_file, len(content))

    # 4. Remove stale container if present
    master_client.run(f"docker rm -f {container} 2>/dev/null", check=False)

    # 5. Ensure the master-local shared jumphost network exists. The DNS
    #    resolver is consumed by the jumphost, while VD access goes through
    #    runtime relays; this must not depend on the lab mgmt network existing
    #    on the master.
    ensure_jumphost_network(master_client, topo.jumphost_net)
    resolver_ip = allocate_jumphost_ip(master_client, topo.jumphost_net).split("/")[0]

    # 6. Start the dnsmasq container
    upstream_env = " ".join(upstream)
    run_cmd = (
        f"docker run -d "
        f"--name {container} "
        f"--network {network} "
        f"--ip {resolver_ip} "
        f"--cap-add NET_ADMIN "
        f"-v {host_file}:/etc/dnlab-dns/hosts:ro "
        f"-e UPSTREAM_DNS='{upstream_env}' "
        f"{image}"
    )
    master_client.run(run_cmd)

    # 7. Verify it's actually running (docker run -d returns success even if
    #    the container dies immediately).
    rc, out, _ = master_client.run_no_check(
        f"docker inspect -f '{{{{.State.Running}}}}' {container}"
    )
    if rc != 0 or out.strip() != "true":
        # Grab logs for diagnostics, then bail.
        _, logs, _ = master_client.run_no_check(f"docker logs {container} 2>&1 | tail -40")
        master_client.run(f"docker rm -f {container} 2>/dev/null", check=False)
        raise RuntimeError(
            f"DNS container '{container}' failed to start.\n"
            f"--- docker logs (last 40 lines) ---\n{logs}\n"
            f"-----------------------------------"
        )

    log.info("DNS container running: %s → %s (upstream: %s)",
             container, resolver_ip, upstream)

    return container, resolver_ip, upstream, len(entries)


def allocate_dns_resolver_ip(topo: DistributedTopology, master_client: SSHClient) -> str:
    """Pick a resolver IP on the shared jumphost network."""
    ensure_jumphost_network(master_client, topo.jumphost_net)
    return allocate_jumphost_ip(master_client, topo.jumphost_net).split("/")[0]


def refresh_dns(
    lab_name: str,
    master_client: SSHClient,
    clients: dict[str, SSHClient],
    extra_entries: list[HostEntry] | None = None,
) -> tuple[int, list[HostEntry]]:
    """Regenerate the hosts file and SIGHUP the DNS container.

    Returns: (entry_count, entries)
    """
    container = dns_container_name(lab_name)
    host_file = hosts_file_path(lab_name)

    # Check the container is actually running
    rc, _, _ = master_client.run_no_check(
        f"docker inspect -f '{{{{.State.Running}}}}' {container}"
    )
    if rc != 0:
        raise RuntimeError(
            f"DNS container '{container}' not found on master — "
            f"is the lab deployed?"
        )

    entries = collect_hosts_entries(lab_name, clients)
    if extra_entries:
        entries = _merge_entries(entries, extra_entries)
    content = render_hosts_file(entries)
    master_client.upload_text(content, host_file)
    log.info("DNS hosts file rewritten: %d entries", len(entries))

    # SIGHUP dnsmasq to reload addn-hosts
    master_client.run(f"docker kill -s HUP {container}")
    log.info("DNS container reloaded via SIGHUP: %s", container)

    return len(entries), entries


def _merge_entries(
    base: list[HostEntry],
    extra: list[HostEntry],
) -> list[HostEntry]:
    """Merge DNS entries, keeping the first IP seen for each name."""
    seen: dict[str, HostEntry] = {}
    for entry in [*base, *extra]:
        if entry.name in seen:
            existing = seen[entry.name]
            if existing.ip != entry.ip:
                log.warning(
                    "Duplicate DNS name '%s': %s kept, %s ignored",
                    entry.name, existing.ip, entry.ip,
                )
            continue
        seen[entry.name] = entry
    return list(seen.values())


def destroy_dns(lab_name: str, master_client: SSHClient) -> None:
    """Remove the DNS container and its temporary hosts file."""
    container = dns_container_name(lab_name)
    host_file = hosts_file_path(lab_name)

    log.info("Removing DNS container: %s", container)
    master_client.run(f"docker rm -f {container} 2>/dev/null", check=False)
    master_client.run(f"rm -f {host_file}", check=False)
    log.info("DNS container removed")
