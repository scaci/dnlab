"""Scheduling algorithm — First Fit Decreasing with master preference."""

from __future__ import annotations

import logging

from dnlab_multinode.models.schedule import (
    VDResources, HostResources, HostAssignment, CrossHostLink, SchedulePlan,
)
from dnlab_multinode.models.topology import DistributedTopology, Link
from dnlab_multinode.utils import ids, naming

log = logging.getLogger(__name__)


class ScheduleError(Exception):
    pass


def compute_schedule(
    topo: DistributedTopology,
    vd_resources: dict[str, VDResources],
    host_resources: dict[str, HostResources],
    placement_preferences: dict[str, str] | None = None,
) -> SchedulePlan:
    """Compute the scheduling plan.

    Args:
        topo: parsed distributed topology
        vd_resources: {node_name: VDResources}
        host_resources: {host_name: HostResources}
        placement_preferences: optional {vd_name: host_name} sticky hints

    Returns: SchedulePlan
    """
    placement_preferences = placement_preferences or {}
    # ── Feasibility check ────────────────────────────────────────────
    total_cpu_needed = sum(r.cpu for r in vd_resources.values())
    total_ram_needed = sum(r.ram_mb for r in vd_resources.values())
    total_cpu_avail = sum(h.cpu_available for h in host_resources.values())
    total_ram_avail = sum(h.ram_mb_available for h in host_resources.values())

    if total_cpu_needed > total_cpu_avail or total_ram_needed > total_ram_avail:
        raise ScheduleError(
            f"Insufficient resources for deployment:\n"
            f"  Richiesto:   {total_cpu_needed} CPU, {total_ram_needed} MB RAM\n"
            f"  Available: {total_cpu_avail} CPU, {total_ram_avail} MB RAM\n"
            f"  Add at least 1 worker or reduce the topology"
        )

    # ── Sort VDs by weight (descending) ──────────────────────────────
    sorted_vds = sorted(vd_resources.values(), key=lambda v: v.weight, reverse=True)
    avg_weight = sum(v.weight for v in sorted_vds) / len(sorted_vds) if sorted_vds else 0

    log.debug("VD weights: %s", [(v.name, v.weight) for v in sorted_vds])
    log.debug("Average weight: %d", avg_weight)

    # ── Init assignments with remaining capacity ─────────────────────
    assignments: dict[str, HostAssignment] = {}
    remaining_cpu: dict[str, int] = {}
    remaining_ram: dict[str, int] = {}

    for hname, hr in host_resources.items():
        assignments[hname] = HostAssignment(
            host_name=hname,
            host_ip=hr.host,
        )
        remaining_cpu[hname] = hr.cpu_available
        remaining_ram[hname] = hr.ram_mb_available

    # ── FFD assignment ───────────────────────────────────────────────
    for vd in sorted_vds:
        best_host = None

        def can_fit(hname: str) -> bool:
            return remaining_cpu[hname] >= vd.cpu and remaining_ram[hname] >= vd.ram_mb

        def candidate_key(hname: str) -> tuple[int, int, int, str]:
            assignment = assignments[hname]
            # Lower VD count first, then higher residual capacity. The
            # host name keeps ties deterministic.
            return (
                -len(assignment.vd_names),
                remaining_ram[hname],
                remaining_cpu[hname],
                hname,
            )

        preferred = placement_preferences.get(vd.name)
        if preferred in host_resources and can_fit(preferred):
            best_host = preferred
            log.debug("Sticky placement kept for %s -> %s", vd.name, preferred)
        else:
            worker_candidates = [
                hname for hname, hr in host_resources.items()
                if not hr.is_master and can_fit(hname)
            ]
            if worker_candidates:
                best_host = max(worker_candidates, key=candidate_key)
            else:
                master_candidates = [
                    hname for hname, hr in host_resources.items()
                    if hr.is_master and can_fit(hname)
                ]
                if master_candidates:
                    best_host = max(master_candidates, key=candidate_key)

        if best_host is None:
            raise ScheduleError(
                f"Impossibile assegnare VD '{vd.name}' ({vd.cpu} CPU, {vd.ram_mb} MB): "
                f"no host with enough resources"
            )

        assignments[best_host].vd_names.append(vd.name)
        assignments[best_host].cpu_used += vd.cpu
        assignments[best_host].ram_mb_used += vd.ram_mb
        remaining_cpu[best_host] -= vd.cpu
        remaining_ram[best_host] -= vd.ram_mb

        log.debug("Assigned %s → %s (remaining: %d CPU, %d MB)",
                  vd.name, best_host, remaining_cpu[best_host], remaining_ram[best_host])

    # ── Classify links ───────────────────────────────────────────────
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments=assignments,
        vrf_table_id=ids.vrf_table_id(topo.name),
        mgmt_vxlan_id=ids.mgmt_vxlan_id(topo.name),
    )

    vxlan_base = ids.dataplane_vxlan_base(topo.name)
    vxlan_counter = 0

    # Track all host-iface names per host for uniqueness
    iface_names_per_host: dict[str, list[str]] = {h: [] for h in assignments}

    for link in topo.links:
        src_host = plan.host_for_vd(link.source)
        tgt_host = plan.host_for_vd(link.target)

        if src_host == tgt_host:
            plan.local_links.append(link)
        else:
            vxlan_id = vxlan_base + vxlan_counter
            vxlan_counter += 1

            src_iface = naming.vxlan_host_iface(topo.name, link.source, link.source_iface)
            tgt_iface = naming.vxlan_host_iface(topo.name, link.target, link.target_iface)

            iface_names_per_host.setdefault(src_host, []).append(src_iface)
            iface_names_per_host.setdefault(tgt_host, []).append(tgt_iface)

            plan.cross_host_links.append(CrossHostLink(
                vxlan_id=vxlan_id,
                source_node=link.source,
                source_iface=link.source_iface,
                target_node=link.target,
                target_iface=link.target_iface,
                source_host=src_host,
                target_host=tgt_host,
                source_host_iface=src_iface,
                target_host_iface=tgt_iface,
            ))

    # Ensure interface name uniqueness per host
    for host, inames in iface_names_per_host.items():
        unique = naming.ensure_unique(inames)
        idx = 0
        for cl in plan.cross_host_links:
            if cl.source_host == host:
                cl.source_host_iface = unique[idx]
                idx += 1
            if cl.target_host == host:
                cl.target_host_iface = unique[idx]
                idx += 1

    for rn_link in topo.real_net_links:
        host = plan.host_for_vd(rn_link.node)
        if not host:
            raise ScheduleError(
                f"real_net '{rn_link.real_net}' references unscheduled node '{rn_link.node}'"
            )
        rn_link.host = host
        rn_link.bridge_iface = naming.realnet_bridge_iface(rn_link.node, rn_link.iface)

    log.info("Schedule: %d local links, %d cross-host links",
             len(plan.local_links), len(plan.cross_host_links))

    return plan


def gather_host_resources(ssh_clients: dict) -> dict[str, HostResources]:
    """Query each host for available CPU and RAM.

    Args:
        ssh_clients: {host_name: SSHClient} (must be connected)

    Returns: {host_name: HostResources}
    """
    results = {}
    for name, client in ssh_clients.items():
        try:
            cpu_str = client.run("nproc")
            ram_str = client.run("free -m | awk '/^Mem:/{print $7}'")
            cpu = int(cpu_str.strip())
            ram_mb = int(ram_str.strip())

            results[name] = HostResources(
                name=name,
                host=client.host,
                cpu_available=cpu,
                ram_mb_available=ram_mb,
                is_master=(name == "master"),
            )
            log.info("[%s] Resources: %d CPU, %d MB RAM available", name, cpu, ram_mb)
        except Exception as e:
            log.error("[%s] Cannot query resources: %s", name, e)
            raise ScheduleError(f"Node '{name}' ({client.host}) unreachable or error: {e}")

    return results
