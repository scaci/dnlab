"""Per-VD runtime link reconciliation."""

from __future__ import annotations

import hashlib
import logging
import shlex

from dnlab_multinode.models.schedule import SchedulePlan
from dnlab_multinode.models.state import RuntimeLinkState
from dnlab_multinode.models.topology import DistributedTopology
from dnlab_multinode.services.ssh import SSHClient
from dnlab_multinode.utils import naming

log = logging.getLogger(__name__)


def build_runtime_links(topo: DistributedTopology, plan: SchedulePlan) -> list[RuntimeLinkState]:
    links: list[RuntimeLinkState] = []

    for idx, link in enumerate(plan.local_links):
        link_id = f"l{idx}"
        host = plan.host_for_vd(link.source) or ""
        links.append(RuntimeLinkState(
            id=link_id,
            link_type="same_host",
            endpoint_a={"node": link.source, "iface": link.source_iface},
            endpoint_b={"node": link.target, "iface": link.target_iface},
            host_a=host,
            host_b=host,
            host_endpoint_a=naming.runtime_host_endpoint(topo.name, link.source, link.source_iface, link_id),
            host_endpoint_b=naming.runtime_host_endpoint(topo.name, link.target, link.target_iface, link_id),
            state="down",
        ))

    for idx, link in enumerate(plan.cross_host_links):
        link_id = f"vx{idx}"
        links.append(RuntimeLinkState(
            id=link_id,
            link_type="cross_host",
            endpoint_a={"node": link.source_node, "iface": link.source_iface},
            endpoint_b={"node": link.target_node, "iface": link.target_iface},
            host_a=link.source_host,
            host_b=link.target_host,
            host_endpoint_a=link.source_host_iface,
            host_endpoint_b=link.target_host_iface,
            vxlan_id=link.vxlan_id,
            state="down",
        ))

    for idx, link in enumerate(topo.real_net_links):
        link_id = f"rn{idx}"
        bridge = naming.realnet_bridge_name(topo.name, link.real_net)
        links.append(RuntimeLinkState(
            id=link_id,
            link_type="real_net",
            endpoint_a={"node": link.node, "iface": link.iface},
            endpoint_b={"real_net": link.real_net},
            host_a=link.host,
            host_b=link.host,
            host_endpoint_a=link.bridge_iface or naming.realnet_bridge_iface(link.node, link.iface),
            host_endpoint_b=bridge,
            state="down",
        ))

    return links


def create_link(
    link: RuntimeLinkState,
    clients: dict[str, SSHClient],
    underlay_ips: dict[str, str] | None = None,
    running_nodes: set[str] | None = None,
    container_names: dict[str, str] | None = None,
) -> RuntimeLinkState:
    """Create one runtime link if both VD endpoints are running."""
    underlay_ips = underlay_ips or {}
    running_nodes = running_nodes or _nodes_in_link(link)
    missing = _stopped_endpoints(link, running_nodes)
    if missing:
        link.state = "partial"
        link.last_error = f"endpoint stopped: {', '.join(sorted(missing))}"
        return link

    try:
        if link.link_type == "same_host":
            _create_same_host(link, clients)
        elif link.link_type == "cross_host":
            _create_cross_host(link, clients, underlay_ips, container_names)
        elif link.link_type == "real_net":
            _create_realnet(link, clients, container_names)
        else:
            raise ValueError(f"unsupported link_type {link.link_type!r}")
        link.state = "up"
        link.last_error = ""
    except Exception as exc:
        link.state = "error"
        link.last_error = str(exc)
        raise
    return link


def delete_link(link: RuntimeLinkState, clients: dict[str, SSHClient]) -> RuntimeLinkState:
    try:
        if link.link_type == "same_host":
            _delete_same_host(link, clients)
        elif link.link_type == "cross_host":
            _delete_cross_host(link, clients)
        elif link.link_type == "real_net":
            _delete_realnet(link, clients)
        link.state = "down"
        link.last_error = ""
    except Exception as exc:
        link.state = "error"
        link.last_error = str(exc)
        raise
    return link


def delete_node_links(
    node: str,
    links: list[RuntimeLinkState],
    clients: dict[str, SSHClient],
) -> list[RuntimeLinkState]:
    touched = []
    for link in links:
        if node in _nodes_in_link(link):
            touched.append(delete_link(link, clients))
    return touched


def reconcile_all_links(
    links: list[RuntimeLinkState],
    clients: dict[str, SSHClient],
    underlay_ips: dict[str, str],
    running_nodes: set[str],
    container_names: dict[str, str] | None = None,
) -> list[RuntimeLinkState]:
    reconciled = []
    for link in links:
        reconciled.append(create_link(
            link, clients, underlay_ips, running_nodes, container_names,
        ))
    return reconciled


def reconcile_node_links(
    node: str,
    links: list[RuntimeLinkState],
    clients: dict[str, SSHClient],
    underlay_ips: dict[str, str],
    running_nodes: set[str],
    container_names: dict[str, str] | None = None,
) -> list[RuntimeLinkState]:
    reconciled = []
    for link in links:
        if node in _nodes_in_link(link):
            reconciled.append(create_link(
                link, clients, underlay_ips, running_nodes, container_names,
            ))
    return reconciled


def _create_same_host(link: RuntimeLinkState, clients: dict[str, SSHClient]) -> None:
    client = clients[link.host_a]
    bridge = _runtime_bridge_name(link)
    client.run(f"ip link show {bridge} >/dev/null 2>&1 || ip link add {bridge} type bridge")
    client.run(f"ip link set {bridge} up")
    _ensure_bridge_forwarding(client, bridge)
    for iface in [link.host_endpoint_a, link.host_endpoint_b]:
        client.run(f"ip link set {iface} up")
        client.run(f"ip link set {iface} master {bridge}")


def _delete_same_host(link: RuntimeLinkState, clients: dict[str, SSHClient]) -> None:
    client = clients.get(link.host_a)
    if client:
        bridge = _runtime_bridge_name(link)
        _remove_bridge_forwarding(client, bridge)
        client.run(f"ip link delete {bridge} 2>/dev/null", check=False)


def _create_cross_host(
    link: RuntimeLinkState,
    clients: dict[str, SSHClient],
    underlay_ips: dict[str, str],
    container_names: dict[str, str] | None,
) -> None:
    src = clients[link.host_a]
    dst = clients[link.host_b]
    if container_names is not None:
        _ensure_host_endpoint(
            src, link.endpoint_a, link.host_endpoint_a, container_names,
        )
        _ensure_host_endpoint(
            dst, link.endpoint_b, link.host_endpoint_b, container_names,
        )
    src.run(
        f"ip link show {_vxlan_altname(link.host_endpoint_a)} >/dev/null 2>&1 || "
        f"containerlab tools vxlan create --remote {underlay_ips[link.host_b]} "
        f"--id {link.vxlan_id} --link {link.host_endpoint_a}"
    )
    dst.run(
        f"ip link show {_vxlan_altname(link.host_endpoint_b)} >/dev/null 2>&1 || "
        f"containerlab tools vxlan create --remote {underlay_ips[link.host_a]} "
        f"--id {link.vxlan_id} --link {link.host_endpoint_b}"
    )


def _ensure_host_endpoint(
    client: SSHClient,
    endpoint: dict[str, str],
    host_iface: str,
    container_names: dict[str, str],
) -> None:
    """Restore a missing Containerlab node-to-host veth before VxLAN attach.

    Containerlab 0.77 may return success while a special ``host`` endpoint
    failed to materialize.  The supported ``tools veth`` primitive makes the
    desired topology convergent without recreating the VD or its persistent
    disk.
    """
    rc, _, _ = client.run_no_check(
        f"ip link show {shlex.quote(host_iface)} >/dev/null 2>&1"
    )
    if rc == 0:
        return

    node = endpoint.get("node", "")
    iface = endpoint.get("iface", "")
    container = container_names.get(node, "")
    if not node or not iface or not container:
        raise RuntimeError(
            f"cannot restore host endpoint {host_iface!r}: "
            f"missing runtime container for node {node!r}"
        )

    client.run(
        "containerlab tools veth create "
        f"--a-endpoint {shlex.quote(f'{container}:{iface}')} "
        f"--b-endpoint {shlex.quote(f'host:{host_iface}')}",
    )
    rc, _, err = client.run_no_check(
        f"ip link show {shlex.quote(host_iface)} >/dev/null 2>&1"
    )
    if rc != 0:
        raise RuntimeError(
            f"Containerlab reported success but host endpoint {host_iface!r} "
            f"is still missing: {err.strip()}"
        )


def _delete_cross_host(link: RuntimeLinkState, clients: dict[str, SSHClient]) -> None:
    for host, iface in [
        (link.host_a, link.host_endpoint_a),
        (link.host_b, link.host_endpoint_b),
    ]:
        client = clients.get(host)
        if client:
            client.run(f"ip link delete {_vxlan_altname(iface)} 2>/dev/null", check=False)
            client.run(f"ip link delete {iface} 2>/dev/null", check=False)


def _create_realnet(
    link: RuntimeLinkState,
    clients: dict[str, SSHClient],
    container_names: dict[str, str] | None,
) -> None:
    client = clients[link.host_a]
    if container_names is not None:
        _ensure_host_endpoint(
            client, link.endpoint_a, link.host_endpoint_a, container_names,
        )
    client.run(f"ip link set {link.host_endpoint_a} up")
    client.run(f"ip link set {link.host_endpoint_a} master {link.host_endpoint_b}")


def _delete_realnet(link: RuntimeLinkState, clients: dict[str, SSHClient]) -> None:
    client = clients.get(link.host_a)
    if client:
        client.run(
            f"ip link delete {link.host_endpoint_a} 2>/dev/null", check=False,
        )


def _nodes_in_link(link: RuntimeLinkState) -> set[str]:
    nodes = set()
    for endpoint in [link.endpoint_a, link.endpoint_b]:
        node = endpoint.get("node")
        if node:
            nodes.add(node)
    return nodes


def _stopped_endpoints(link: RuntimeLinkState, running_nodes: set[str]) -> set[str]:
    return {node for node in _nodes_in_link(link) if node not in running_nodes}


def _runtime_bridge_name(link: RuntimeLinkState) -> str:
    digest = hashlib.sha1(link.id.encode()).hexdigest()[:8]
    return f"br-rt-{digest}"[:15]


def _vxlan_altname(host_endpoint: str) -> str:
    # The real kernel ifname remains containerlab's short unique clab-<hash>.
    # This requested name is stored as an altname, which ip-link can resolve for
    # idempotency checks and cleanup without violating the 15-char ifname limit.
    return f"vx-{host_endpoint}"


def _ensure_bridge_forwarding(client: SSHClient, bridge: str) -> None:
    """Allow bridged IP traffic on runtime bridges when br_netfilter is on."""
    comment = f"dnlab runtime {bridge}"
    client.run(
        f"iptables -C FORWARD -i {bridge} -m comment --comment '{comment}' -j ACCEPT "
        f"2>/dev/null || iptables -I FORWARD 1 -i {bridge} "
        f"-m comment --comment '{comment}' -j ACCEPT; "
        f"iptables -C FORWARD -o {bridge} -m comment --comment '{comment}' -j ACCEPT "
        f"2>/dev/null || iptables -I FORWARD 1 -o {bridge} "
        f"-m comment --comment '{comment}' -j ACCEPT",
        check=False,
    )


def _remove_bridge_forwarding(client: SSHClient, bridge: str) -> None:
    comment = f"dnlab runtime {bridge}"
    client.run(
        f"while iptables -C FORWARD -i {bridge} -m comment --comment '{comment}' "
        f"-j ACCEPT 2>/dev/null; do "
        f"iptables -D FORWARD -i {bridge} -m comment --comment '{comment}' -j ACCEPT; "
        f"done; "
        f"while iptables -C FORWARD -o {bridge} -m comment --comment '{comment}' "
        f"-j ACCEPT 2>/dev/null; do "
        f"iptables -D FORWARD -o {bridge} -m comment --comment '{comment}' -j ACCEPT; "
        f"done",
        check=False,
    )
