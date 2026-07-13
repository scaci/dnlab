"""Status controller — read-only live inspection of a deployed lab.

Exposes :class:`StatusController`, used by both the CLI ``get-status``
command and the GUI. It merges three sources of truth:

* the declared topology (what should exist),
* the saved deployment state (what the orchestrator provisioned), and
* live ``docker ps`` output on each host (what's running right now).

The controller does NOT mutate any remote state. It is safe to call
concurrently with other controllers and can be polled by the GUI.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dnlab_multinode.models.state import DeploymentState
from dnlab_multinode.models.topology import DistributedTopology
from dnlab_multinode.services import state as state_svc
from dnlab_multinode.services.clab_capabilities import PER_HOST_APPLY
from dnlab_multinode.services.config import assign_sticky_mgmt_ipv4, parse_topology
from dnlab_multinode.services.progress import ProgressCallback, make_timer
from dnlab_multinode.services.ssh import SSHClient, create_clients

log = logging.getLogger(__name__)


@dataclass
class NodeStatus:
    name: str
    host: str                  # live infra host name ("master" or worker)
    kind: str
    image: str
    mgmt_ipv4: str
    container: str             # expected container name (clab-<lab>-<node>)
    state: str = "unknown"     # running | exited | missing | unreachable | unknown
    scheduled_host: str = ""   # host recorded in the deployment state
    placement_mismatch: bool = False
    duplicate_hosts: list[str] = field(default_factory=list)
    started_at: str = ""
    uptime_seconds: int = 0
    topology_file: str = ""
    apply_mode: str = ""
    last_error: str = ""
    # Mappa delle Web UI esposte da clab via ``-p``: lista di
    # ``{container_port, host_port, bind_ip, proto}``. Vuota se il
    # nodo non ha Web UI dichiarate o il lab non è deployato.
    webui_ports: list[dict] = field(default_factory=list)
    # Best-effort Containerlab runtime interface view, populated for
    # per-host-apply deployments via ``inspect interfaces --format json``.
    interfaces: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "host": self.host,
            "kind": self.kind,
            "image": self.image,
            "mgmt_ipv4": self.mgmt_ipv4,
            "container": self.container,
            "state": self.state,
            "scheduled_host": self.scheduled_host,
            "placement_mismatch": self.placement_mismatch,
            "duplicate_hosts": self.duplicate_hosts,
            "started_at": self.started_at,
            "uptime_seconds": self.uptime_seconds,
            "topology_file": self.topology_file,
            "apply_mode": self.apply_mode,
            "last_error": self.last_error,
            "webui_ports": self.webui_ports,
            "interfaces": self.interfaces,
        }


@dataclass
class HostStatus:
    name: str
    host: str                  # IP
    reachable: bool = False
    error: str = ""
    vd_count: int = 0
    live_vd_count: int = 0
    cpu_used: int = 0
    ram_mb_used: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "host": self.host,
            "reachable": self.reachable,
            "error": self.error,
            "vd_count": self.vd_count,
            "live_vd_count": self.live_vd_count,
            "cpu_used": self.cpu_used,
            "ram_mb_used": self.ram_mb_used,
        }


@dataclass
class InfraStatus:
    dns: dict = field(default_factory=dict)
    jumphost: dict = field(default_factory=dict)
    runtime_relays: dict = field(default_factory=dict)
    realnets: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "dns": self.dns,
            "jumphost": self.jumphost,
            "runtime_relays": self.runtime_relays,
            "realnets": self.realnets,
        }


@dataclass
class StatusReport:
    lab_name: str
    deployed: bool
    dnlab_deployed: bool = False
    deployed_at: str = ""
    runtime_mode: str = ""
    containerlab_versions: dict[str, str] = field(default_factory=dict)
    host_apply_status: dict[str, str] = field(default_factory=dict)
    host_apply_plan: dict[str, list[dict]] = field(default_factory=dict)
    reconcile_required: bool = False
    hosts: dict[str, HostStatus] = field(default_factory=dict)
    nodes: dict[str, NodeStatus] = field(default_factory=dict)
    cross_host_links: int = 0
    runtime_links: list[dict] = field(default_factory=list)
    infra: InfraStatus = field(default_factory=InfraStatus)

    def to_dict(self) -> dict:
        return {
            "lab_name": self.lab_name,
            "deployed": self.deployed,
            "dnlab_deployed": self.dnlab_deployed,
            "deployed_at": self.deployed_at,
            "runtime_mode": self.runtime_mode,
            "containerlab_versions": self.containerlab_versions,
            "host_apply_status": self.host_apply_status,
            "host_apply_plan": self.host_apply_plan,
            "reconcile_required": self.reconcile_required,
            "hosts": {n: h.to_dict() for n, h in self.hosts.items()},
            "nodes": {n: v.to_dict() for n, v in self.nodes.items()},
            "cross_host_links": self.cross_host_links,
            "runtime_links": self.runtime_links,
            "infra": self.infra.to_dict(),
        }


class StatusController:
    """Read-only controller producing a :class:`StatusReport`."""

    def __init__(
        self,
        topology_file: str,
        *,
        hosts_file: str | None = None,
        progress: ProgressCallback | None = None,
    ):
        self.topology_file = topology_file
        self.hosts_file = hosts_file
        self._progress = make_timer(progress)

    def run(self) -> StatusReport:
        self._progress.emit("status", "start", detail="Collecting status")

        topo = parse_topology(self.topology_file, hosts_file=self.hosts_file)
        state_dir = Path(self.topology_file).parent
        state = state_svc.load_state(topo.name, state_dir)
        if state is not None:
            assign_sticky_mgmt_ipv4(
                topo.nodes,
                topo.mgmt,
                state.mgmt_ip_reservations,
            )

        report = StatusReport(
            lab_name=topo.name,
            deployed=state is not None,
            dnlab_deployed=state.dnlab_deployed if state else False,
            deployed_at=state.deployed_at if state else "",
            runtime_mode=state.runtime_mode if state else "",
            containerlab_versions=dict(state.containerlab_versions) if state else {},
            host_apply_status=dict(state.host_apply_status) if state else {},
            host_apply_plan=dict(state.host_apply_plan) if state else {},
            reconcile_required=state.reconcile_required if state else False,
        )

        self._collect_host_summary(topo, state, report)

        if state is None:
            self._progress.emit(
                "status", "ok",
                detail=f"Lab '{topo.name}' not deployed",
            )
            for name, vd in topo.nodes.items():
                report.nodes[name] = NodeStatus(
                    name=name,
                    host="",
                    kind=vd.kind,
                    image=vd.image,
                    mgmt_ipv4=vd.mgmt_ipv4,
                    container=f"clab-{topo.name}-{name}",
                    state="missing",
                )
            return report

        clients = create_clients(topo.all_hosts)
        try:
            for host_name, client in clients.items():
                try:
                    client.connect()
                    report.hosts[host_name].reachable = True
                except Exception as e:
                    report.hosts[host_name].reachable = False
                    report.hosts[host_name].error = str(e)
                    self._progress.emit(
                        "status-connect", "error",
                        host=host_name, detail=str(e),
                    )

            self._collect_node_status(topo, state, clients, report)
            self._collect_interface_status(state, clients, report)
            self._collect_infra_status(state, clients, report)

            report.cross_host_links = len(state.vxlan_dataplane)
            report.runtime_links = [
                {
                    "id": link.id,
                    "type": link.link_type,
                    "state": link.state,
                    "endpoint_a": link.endpoint_a,
                    "endpoint_b": link.endpoint_b,
                    "host_a": link.host_a,
                    "host_b": link.host_b,
                    "host_endpoint_a": link.host_endpoint_a,
                    "host_endpoint_b": link.host_endpoint_b,
                    "vxlan_id": link.vxlan_id,
                    "last_error": link.last_error,
                }
                for link in state.runtime_links
            ]
        finally:
            for client in clients.values():
                client.close()

        self._progress.emit(
            "status", "ok",
            detail=f"Status for '{topo.name}' collected",
            data={"deployed": report.deployed, "nodes": len(report.nodes)},
        )
        return report

    # ── host summary ────────────────────────────────────────────────

    def _collect_host_summary(
        self,
        topo: DistributedTopology,
        state: DeploymentState | None,
        report: StatusReport,
    ) -> None:
        for host_name, h in topo.all_hosts.items():
            hs = HostStatus(name=host_name, host=h.host)
            if state and host_name in state.scheduling:
                sched = state.scheduling[host_name]
                hs.vd_count = len(sched.vd)
                hs.cpu_used = sched.resources_used.get("cpu", 0)
                hs.ram_mb_used = sched.resources_used.get("ram_mb", 0)
            report.hosts[host_name] = hs

    # ── VD status ───────────────────────────────────────────────────

    def _collect_node_status(
        self,
        topo: DistributedTopology,
        state: DeploymentState,
        clients: dict[str, SSHClient],
        report: StatusReport,
    ) -> None:
        # Reverse index: vd_name → host_name
        vd_host: dict[str, str] = {}
        for host_name, sched in state.scheduling.items():
            for vd in sched.vd:
                vd_host[vd] = host_name

        # Gather `docker ps` output per host in parallel
        per_host: dict[str, dict[str, dict]] = {}

        def _probe(host_name: str) -> tuple[str, dict[str, dict]]:
            client = clients.get(host_name)
            if not client or not report.hosts[host_name].reachable:
                return host_name, {}
            cmd = (
                "docker ps -a --format '{{.Names}}\\t{{.State}}\\t{{.Status}}'"
            )
            rc, out, _ = client.run_no_check(cmd, timeout=15)
            if rc != 0:
                return host_name, {}
            parsed: dict[str, dict] = {}
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                name, state_str, status_str = parts[0], parts[1], parts[2]
                parsed[name] = {"state": state_str, "status": status_str}
            return host_name, parsed

        reachable_hosts = [h for h, hs in report.hosts.items() if hs.reachable]
        if reachable_hosts:
            with ThreadPoolExecutor(max_workers=len(reachable_hosts)) as pool:
                futures = [pool.submit(_probe, h) for h in reachable_hosts]
                for f in as_completed(futures):
                    host_name, parsed = f.result()
                    per_host[host_name] = parsed

        for name, vd in topo.nodes.items():
            runtime = state.node_runtime.get(name)
            container = runtime.container if runtime else f"clab-{topo.name}-{name}"
            scheduled_host = runtime.host if runtime else vd_host.get(name, "")
            live_hits = [
                (host, per_host[host][container])
                for host in sorted(per_host)
                if container in per_host[host]
            ]
            live_hosts = [host for host, _ in live_hits]
            duplicate_hosts = live_hosts if len(live_hosts) > 1 else []
            live_info: dict | None = None

            if len(live_hits) == 1:
                host_name, live_info = live_hits[0]
            elif len(live_hits) > 1:
                host_name, live_info = next(
                    (
                        (host, info)
                        for host, info in live_hits
                        if host == scheduled_host
                    ),
                    live_hits[0],
                )
                log.warning(
                    "Duplicate container %s found on hosts %s; reporting %s",
                    container, ", ".join(live_hosts), host_name,
                )
            else:
                host_name = scheduled_host

            ns = NodeStatus(
                name=name,
                host=host_name,
                kind=vd.kind,
                image=vd.image,
                mgmt_ipv4=vd.mgmt_ipv4,
                container=container,
                scheduled_host=scheduled_host,
                duplicate_hosts=duplicate_hosts,
                topology_file=runtime.topology_file if runtime else "",
                apply_mode=runtime.apply_mode if runtime else "",
                last_error=runtime.last_error if runtime else "",
            )

            if live_info is not None:
                ns.state = live_info["state"]
                ns.started_at = live_info["status"]
            elif runtime and runtime.state in {"stopped", "stopping", "starting", "error"}:
                ns.state = runtime.state
                ns.started_at = runtime.started_at
            elif not host_name:
                ns.state = "missing"
            elif not report.hosts.get(host_name, HostStatus(name="", host="")).reachable:
                ns.state = "unreachable"
            else:
                ns.state = "missing"

            ns.placement_mismatch = (
                bool(duplicate_hosts)
                or (
                    bool(scheduled_host)
                    and bool(host_name)
                    and host_name != scheduled_host
                )
            )
            # Web UI host ports allocate al deploy (sticky cross-deploy).
            ns.webui_ports = [
                {
                    "container_port": a.container_port,
                    "host_port":      a.host_port,
                    "bind_ip":        a.bind_ip,
                    "proto":          a.proto,
                }
                for a in (state.webui_allocations or {}).get(name, [])
            ]
            report.nodes[name] = ns

        for host_name, parsed in per_host.items():
            if host_name in report.hosts:
                report.hosts[host_name].live_vd_count = sum(
                    1
                    for container in parsed
                    if (
                        container.startswith(f"clab-{topo.name}-")
                        or container.startswith(f"clab-dnlab-{topo.name}-")
                    )
                )

        # Best-effort uptime derivation when docker reports a "Up X" status.
        now = datetime.now(timezone.utc)
        for ns in report.nodes.values():
            if ns.state == "running" and ns.started_at.startswith("Up "):
                ns.uptime_seconds = _parse_docker_uptime(ns.started_at)
            elif ns.state == "running":
                ns.uptime_seconds = 0
            _ = now  # placeholder; exact uptime needs inspect, skipped for perf

    def _collect_interface_status(
        self,
        state: DeploymentState,
        clients: dict[str, SSHClient],
        report: StatusReport,
    ) -> None:
        if state.runtime_mode != PER_HOST_APPLY:
            return

        topology_by_host: dict[str, str] = {}
        for node, runtime in state.node_runtime.items():
            if runtime.host and runtime.topology_file and node in report.nodes:
                topology_by_host.setdefault(runtime.host, runtime.topology_file)

        for host_name, topology_file in topology_by_host.items():
            host_status = report.hosts.get(host_name)
            client = clients.get(host_name)
            if not client or not host_status or not host_status.reachable:
                continue
            try:
                raw = client.inspect_clab_interfaces(topology_file)
                data = json.loads(raw or "[]")
                by_node = _parse_clab_interfaces(data, set(report.nodes))
            except Exception as exc:
                log.warning(
                    "Failed to inspect Containerlab interfaces on %s: %s",
                    host_name, exc,
                )
                for ns in report.nodes.values():
                    if ns.scheduled_host == host_name and not ns.last_error:
                        ns.last_error = f"inspect interfaces failed: {exc}"
                continue

            for node, interfaces in by_node.items():
                ns = report.nodes.get(node)
                if ns:
                    ns.interfaces = interfaces

    # ── infra status ───────────────────────────────────────────────

    def _collect_infra_status(
        self,
        state: DeploymentState,
        clients: dict[str, SSHClient],
        report: StatusReport,
    ) -> None:
        if state.dns:
            report.infra.dns = {
                "container": state.dns.container,
                "host": state.dns.node,
                "mgmt_ip": state.dns.mgmt_ip,
                "entries": state.dns.entries,
                "running": _container_running(clients, state.dns.node, state.dns.container),
            }
        if state.jumphost:
            report.infra.jumphost = {
                "container": state.jumphost.container,
                "host": state.jumphost.node,
                "mgmt_ip": state.jumphost.mgmt_ip,
                "ext_ip": state.jumphost.host_ip.split("/")[0] if state.jumphost.host_ip else "",
                "ssh_port": state.jumphost.ssh_port,
                "ssh_bind_ip": state.jumphost.ssh_bind_ip,
                "running": _container_running(clients, state.jumphost.node, state.jumphost.container),
            }
        if state.runtime_relays:
            relays: dict[str, dict] = {}
            for host_name, rr in state.runtime_relays.items():
                relays[host_name] = {
                    "container": rr.container,
                    "host": rr.host,
                    "bind_ip": rr.bind_ip,
                    "port": rr.port,
                    "allowed": len(rr.allowed),
                    "running": _container_running(clients, host_name, rr.container),
                }
            report.infra.runtime_relays = relays
        if state.realnets:
            report.infra.realnets = [
                {
                    "name": rn.name,
                    "bridge": rn.bridge,
                    "router": rn.router_container,
                    "router_wan_ip": rn.router_wan_ip,
                    "lan_ipv4": rn.lan_ipv4,
                    "nat": rn.nat,
                    "bgp": rn.bgp,
                    "bgp_as": rn.bgp_as,
                    "bgp_router_ip": rn.bgp_router_ip,
                    "hosts": rn.hosts,
                    "running": _container_running(clients, "master", rn.router_container),
                }
                for rn in state.realnets
            ]


def _container_running(
    clients: dict[str, SSHClient],
    host_name: str,
    container: str,
) -> bool | None:
    client = clients.get(host_name)
    if not client:
        return None
    try:
        rc, out, _ = client.run_no_check(
            f"docker inspect -f '{{{{.State.Running}}}}' {container}",
            timeout=10,
        )
    except Exception:
        return None
    if rc != 0:
        return False
    return out.strip().lower() == "true"


def _parse_docker_uptime(status: str) -> int:
    """Parse ``Up 12 minutes`` → approximate seconds. Best-effort."""
    import re
    m = re.match(r"Up\s+(?:About\s+)?(\d+)\s+(second|minute|hour|day|week)s?", status)
    if not m:
        return 0
    n = int(m.group(1))
    unit = m.group(2)
    mult = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}[unit]
    return n * mult


def _parse_clab_interfaces(data, known_nodes: set[str]) -> dict[str, list[dict]]:
    parsed: dict[str, list[dict]] = {}

    def _walk(obj, node_hint: str = "") -> None:
        if isinstance(obj, list):
            for item in obj:
                _walk(item, node_hint)
            return
        if not isinstance(obj, dict):
            return

        node = _node_from_interface_record(obj, node_hint, known_nodes)
        iface = _interface_name_from_record(obj)
        if node and iface:
            parsed.setdefault(node, []).append(_normalise_interface_record(obj, iface))
            return

        for key, value in obj.items():
            next_hint = key if key in known_nodes else node_hint
            _walk(value, next_hint)

    _walk(data)
    for interfaces in parsed.values():
        interfaces.sort(key=lambda item: item.get("name", ""))
    return parsed


def _node_from_interface_record(
    record: dict,
    node_hint: str,
    known_nodes: set[str],
) -> str:
    candidates = [
        node_hint,
        str(record.get("node") or ""),
        str(record.get("node_name") or ""),
        str(record.get("nodeName") or ""),
        str(record.get("container") or ""),
        str(record.get("container_name") or ""),
        str(record.get("containerName") or ""),
    ]
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        if candidate in known_nodes:
            return candidate
        for node in known_nodes:
            if candidate.endswith(f"-{node}"):
                return node
    return ""


def _interface_name_from_record(record: dict) -> str:
    for key in ("interface", "interface_name", "interfaceName", "ifname", "name"):
        value = record.get(key)
        if value is not None:
            return str(value)
    return ""


def _normalise_interface_record(record: dict, iface: str) -> dict:
    out = {
        "name": iface,
        "alias": str(
            record.get("alias")
            or record.get("interface_alias")
            or record.get("interfaceAlias")
            or ""
        ),
        "state": str(
            record.get("state")
            or record.get("oper_state")
            or record.get("operState")
            or ""
        ),
        "type": str(record.get("type") or ""),
        "mac": str(record.get("mac") or record.get("mac_address") or ""),
        "mtu": record.get("mtu", ""),
        "peer": str(record.get("peer") or record.get("peer_name") or ""),
    }
    return {key: value for key, value in out.items() if value not in ("", None)}
