"""Real network infrastructure for GUI pseudo-node ``_real_net``.

The GUI stores a cloud-like pseudo-node. At deploy time we translate it into:

* one Linux bridge per real_net on every participating host;
* a VXLAN port between each worker bridge and the master bridge;
* one unmanaged router container on the master, attached to the NAT Docker
  WAN network in NAT mode, or to the host-side BGP segment via ipvlan in
  BGP mode, plus the real_net bridge through a veth pair.
"""

from __future__ import annotations

import ipaddress
import hashlib
import logging
import shlex

from dnlab_multinode.models.state import RealNetState
from dnlab_multinode.models.topology import DistributedTopology, RealNet
from dnlab_multinode.services.images import image_for
from dnlab_multinode.services.ssh import SSHClient
from dnlab_multinode.utils import ids, naming

log = logging.getLogger(__name__)


def participating_hosts(topo: DistributedTopology, real_net: str) -> set[str]:
    return {l.host for l in topo.real_net_links if l.real_net == real_net and l.host}


def _ensure_bridge_forwarding(client: SSHClient, bridge: str) -> None:
    """Allow bridged IP traffic for a per-lab real_net bridge.

    Some hosts run with ``br_netfilter`` enabled and ``FORWARD`` policy DROP.
    ARP still passes in that mode, but IP traffic crossing the Linux bridge is
    evaluated by iptables and gets dropped unless the bridge is explicitly
    allowed. Keep this tied to the lab bridge lifecycle.
    """
    comment = f"dnlab real_net {bridge}"
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
    comment = f"dnlab real_net {bridge}"
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
    # containerlab also adds FORWARD accepts for bridge nodes in generated
    # per-host topologies. They are tied to this per-lab bridge name and can
    # survive some destroy paths, so remove any remaining direct accepts too.
    client.run(
        f"while iptables -C FORWARD -i {bridge} -j ACCEPT 2>/dev/null; do "
        f"iptables -D FORWARD -i {bridge} -j ACCEPT; "
        f"done; "
        f"while iptables -C FORWARD -o {bridge} -j ACCEPT 2>/dev/null; do "
        f"iptables -D FORWARD -o {bridge} -j ACCEPT; "
        f"done",
        check=False,
    )
    client.run(
        f"while iptables -C FORWARD -i {bridge} -m comment --comment 'set by containerlab' "
        f"-j ACCEPT 2>/dev/null; do "
        f"iptables -D FORWARD -i {bridge} -m comment --comment 'set by containerlab' -j ACCEPT; "
        f"done; "
        f"while iptables -C FORWARD -o {bridge} -m comment --comment 'set by containerlab' "
        f"-j ACCEPT 2>/dev/null; do "
        f"iptables -D FORWARD -o {bridge} -m comment --comment 'set by containerlab' -j ACCEPT; "
        f"done",
        check=False,
    )


def ensure_realnet_wan_network(client: SSHClient, topo: DistributedTopology) -> None:
    cfg = topo.realnet_infra
    rc, out, _ = client.run_no_check(
        f"docker network inspect -f '{{{{range .IPAM.Config}}}}{{{{.Subnet}}}}{{{{end}}}}' {cfg.network}"
    )
    if rc == 0:
        current = (out or "").strip()
        if current and current != cfg.ipv4_subnet:
            log.warning(
                "realnet WAN network %s already exists with subnet %s, expected %s",
                cfg.network, current, cfg.ipv4_subnet,
            )
        return
    client.run(
        f"docker network create {cfg.network} "
        f"--driver bridge "
        f"-o com.docker.network.bridge.name={cfg.bridge} "
        f"--subnet {cfg.ipv4_subnet} --gateway {cfg.ipv4_gw}"
    )


def ensure_realnet_bgp_network(client: SSHClient, topo: DistributedTopology) -> str:
    cfg = topo.realnet_infra
    if not cfg.host_net:
        raise ValueError("RealNet BGP requires infrastructure.realnet.host_net")
    network = _bgp_network_name(topo)
    host_net = ipaddress.ip_network(cfg.host_net, strict=False)
    parent = (cfg.wan_iface or "").strip() or _detect_host_iface(client, _host_net_probe_ip(host_net, cfg.rr_ip))
    if not parent:
        raise ValueError("RealNet BGP requires infrastructure.realnet.wan_iface or a routable host_net")
    rc, out, _ = client.run_no_check(
        f"docker network inspect -f '{{{{.Driver}}}} {{{{range .IPAM.Config}}}}{{{{.Subnet}}}}{{{{end}}}}' {network}"
    )
    if rc == 0:
        parts = (out or "").split()
        if len(parts) >= 2 and parts[0] == "macvlan" and parts[1] == str(host_net):
            return network
        raise ValueError(
            f"realnet BGP network {network} already exists with unexpected config: {out}. "
            "Remove/reconcile the RealNet BGP containers and network before redeploying."
        )
    client.run(
        f"docker network create {network} "
        f"--driver macvlan "
        f"-o parent={parent} -o macvlan_mode=bridge "
        f"--subnet {host_net}"
    )
    return network


def setup_bridges(
    topo: DistributedTopology,
    clients: dict[str, SSHClient],
    underlay_ips: dict[str, str],
) -> list[RealNetState]:
    states: list[RealNetState] = []
    master_ip = underlay_ips["master"]
    for idx, (rn_name, rn) in enumerate(topo.real_nets.items()):
        hosts = participating_hosts(topo, rn_name)
        if not hosts:
            continue
        hosts.add("master")
        bridge = naming.realnet_bridge_name(topo.name, rn_name)
        vx_iface = naming.realnet_vxlan_iface(topo.name, rn_name)
        vni = ids.realnet_vxlan_id(topo.name, idx)
        for host in hosts:
            client = clients[host]
            client.run(
                f"ip link show {bridge} >/dev/null 2>&1 || ip link add {bridge} type bridge; "
                f"ip link set {bridge} up"
            )
            _ensure_bridge_forwarding(client, bridge)
        for host in sorted(h for h in hosts if h != "master"):
            client = clients[host]
            client.run(
                f"ip link del {vx_iface} 2>/dev/null || true; "
                f"ip link add {vx_iface} type vxlan id {vni} remote {master_ip} "
                f"local {underlay_ips[host]} dstport 14789 dev {topo.underlay_iface}; "
                f"ip link set {vx_iface} master {bridge}; "
                f"ip link set {vx_iface} up"
            )
            master = clients["master"]
            peer_iface = f"{vx_iface}-{host}"[:15]
            master.run(
                f"ip link del {peer_iface} 2>/dev/null || true; "
                f"ip link add {peer_iface} type vxlan id {vni} remote {underlay_ips[host]} "
                f"local {master_ip} dstport 14789 dev {topo.underlay_iface}; "
                f"ip link set {peer_iface} master {bridge}; "
                f"ip link set {peer_iface} up"
            )
        states.append(RealNetState(
            name=rn_name,
            bridge=bridge,
            vxlan_id=vni,
            hosts=sorted(hosts),
            router_container=naming.realnet_router_container_name(topo.name, rn_name),
            lan_ipv4=rn.ipv4,
            nat=rn.nat and not rn.bgp,
            bgp=rn.bgp,
            bgp_as=rn.bgp_as,
            bgp_router_ip=rn.bgp_router_ip,
        ))
    return states


def deploy_route_reflector(topo: DistributedTopology, master: SSHClient) -> None:
    cfg = topo.realnet_infra
    if not cfg.rr_ip or not cfg.host_net:
        raise ValueError("RealNet BGP requires infrastructure.realnet.rr_ip and host_net")
    host_net = ipaddress.ip_network(cfg.host_net, strict=False)
    if ipaddress.ip_address(cfg.rr_ip) not in host_net:
        raise ValueError(f"RealNet RR IP {cfg.rr_ip} is outside host_net {host_net}")
    image = image_for("realnet-rr")
    rc, _, _ = master.run_no_check(f"docker image inspect {image} >/dev/null 2>&1")
    if rc != 0:
        raise RuntimeError(
            f"RealNet RR image '{image}' not found on master. "
            "Run: docker compose --profile release-images pull"
        )
    master.run("docker rm -f dnlab-realnet-rr 2>/dev/null", check=False)
    master.run(
        f"docker run -d --name dnlab-realnet-rr --hostname realnet-rr "
        f"--privileged --cap-add NET_ADMIN "
        f"--network {ensure_realnet_bgp_network(master, topo)} "
        f"--ip {cfg.rr_ip} "
        f"{image}"
    )
    _write_frr_config(master, "dnlab-realnet-rr", _rr_frr_config(cfg))


def deploy_router(
    topo: DistributedTopology,
    rn: RealNet,
    state: RealNetState,
    master: SSHClient,
) -> RealNetState:
    if not rn.ipv4:
        raise ValueError(f"real_net {rn.name}: missing LAN IPv4 address")
    _ = ipaddress.ip_interface(rn.ipv4)
    image = image_for("realnet-router")
    container = state.router_container
    bridge = state.bridge
    veth_host = naming.realnet_router_veth_name(topo.name, rn.name)
    veth_cont = "eth1"
    nat = "false" if rn.bgp else "true"
    bgp = "true" if rn.bgp else "false"

    rc, _, _ = master.run_no_check(f"docker image inspect {image} >/dev/null 2>&1")
    if rc != 0:
        raise RuntimeError(
            f"RealNet router image '{image}' not found on master. "
            "Run: docker compose --profile release-images pull"
        )

    master.run(f"docker rm -f {container} 2>/dev/null", check=False)
    if rn.bgp:
        if not rn.bgp_as or not rn.bgp_router_ip:
            raise ValueError(f"real_net {rn.name}: missing BGP AS/router IP")
        host_net = ipaddress.ip_network(topo.realnet_infra.host_net, strict=False)
        if ipaddress.ip_address(rn.bgp_router_ip) not in host_net:
            raise ValueError(f"real_net {rn.name}: BGP router IP {rn.bgp_router_ip} is outside host_net {host_net}")
        master.run(
            f"docker run -d --name {container} --hostname {container} "
            f"--privileged --cap-add NET_ADMIN "
            f"--network {ensure_realnet_bgp_network(master, topo)} "
            f"--ip {rn.bgp_router_ip} "
            f"-e REALNET_IPV4={rn.ipv4} "
            f"-e NAT_ENABLED={nat} "
            f"-e BGP_ENABLED={bgp} "
            f"-e BGP_ROUTER_ID={rn.bgp_router_ip} "
            f"{image}"
        )
    else:
        master.run(
            f"docker run -d --name {container} --hostname {container} "
            f"--privileged --cap-add NET_ADMIN "
            f"--network {topo.realnet_infra.network} "
            f"-e REALNET_IPV4={rn.ipv4} "
            f"-e NAT_ENABLED={nat} "
            f"-e BGP_ENABLED={bgp} "
            f"-e BGP_ROUTER_ID={rn.bgp_router_ip} "
            f"{image}"
        )
    pid = master.run(f"docker inspect -f '{{{{.State.Pid}}}}' {container}").strip()
    master.run(
        f"ip link del {veth_host} 2>/dev/null || true; "
        f"ip link add {veth_host} type veth peer name {veth_cont}; "
        f"ip link set {veth_host} master {bridge}; "
        f"ip link set {veth_host} up; "
        f"ip link set {veth_cont} netns {pid}; "
        f"nsenter -t {pid} -n ip link set {veth_cont} name eth1; "
        f"nsenter -t {pid} -n ip link set eth1 up"
    )
    if rn.bgp:
        state.router_wan_ip = rn.bgp_router_ip
        _write_frr_config(master, container, _router_frr_config(topo, rn))
    else:
        wan = master.run(
            f"docker inspect -f '{{{{range .NetworkSettings.Networks}}}}{{{{.IPAddress}}}}{{{{end}}}}' {container}"
        ).strip()
        state.router_wan_ip = wan
    return state


def _bgp_network_name(topo: DistributedTopology) -> str:
    return f"{topo.realnet_infra.network}-bgp"


def _detect_host_iface(client: SSHClient, target_ip: str) -> str:
    rc, out, _ = client.run_no_check(
        "sh -lc "
        + shlex.quote(
            f"ip -o route get {shlex.quote(str(target_ip))} 2>/dev/null "
            "| awk '{for (i=1; i<=NF; i++) if ($i==\"dev\") {print $(i+1); exit}}'"
        )
    )
    return (out or "").strip() if rc == 0 else ""


def _host_net_probe_ip(host_net: ipaddress.IPv4Network | ipaddress.IPv6Network, rr_ip: str) -> str:
    rr = ipaddress.ip_address(rr_ip) if rr_ip else None
    for ip in host_net.hosts():
        if rr is None or ip != rr:
            return str(ip)
    return str(host_net.network_address + 1)


def _rr_frr_config(cfg) -> str:
    return "\n".join([
        "frr defaults traditional",
        "hostname realnet-rr",
        "service integrated-vtysh-config",
        "!",
        "route-map PERMIT_REALNET_ROUTES permit 10",
        " match community HAS-COMMUNITY",
        "exit",
        "!",
        "route-map PERMIT_REALNET_ROUTES permit 20",
        f" set community {cfg.rr_as}:1",
        "exit",
        "!",
        f"router bgp {cfg.rr_as}",
        f" bgp router-id {cfg.rr_ip}",
        " neighbor REALNET_LAB peer-group",
        f" neighbor REALNET_LAB remote-as {cfg.rr_as}",
        *([f" neighbor REALNET_LAB password {cfg.rr_password}"] if getattr(cfg, "rr_password", "") else []),
        f" bgp listen range {cfg.host_net} peer-group REALNET_LAB",
        " !",
        " address-family ipv4 unicast",
        "  neighbor REALNET_LAB route-reflector-client",
        "  neighbor REALNET_LAB route-map PERMIT_REALNET_ROUTES in",
        " exit-address-family",
        "exit",
        "!",
        "bgp community-list expanded HAS-COMMUNITY seq 5 permit .*",
        "!",
        "end",
        "",
    ])


def _router_frr_config(topo: DistributedTopology, rn: RealNet) -> str:
    cfg = topo.realnet_infra
    lan_net = ipaddress.ip_interface(rn.ipv4).network
    lines = [
        "frr defaults traditional",
        f"hostname {rn.name}",
        "service integrated-vtysh-config",
        "!",
        "route-map PERMIT_IN_EBGP permit 10",
        f" set community {rn.bgp_as}:1",
        "exit",
        "!",
        "route-map PERMIT_IN_IBGP permit 10",
        " match community REALNET_PREFIX",
        "exit",
    ]
    for seq, imported in enumerate(rn.import_routers or [], start=20):
        name = _lab_community_name(imported.get("lab_id") or imported.get("id") or "")
        if name:
            lines.extend([
                "!",
                f"route-map PERMIT_IN_IBGP permit {seq}",
                f" match community {name}",
                "exit",
            ])
    lines.extend([
        "!",
        "route-map PERMIT_OUT_EBGP permit 10",
        " set community none",
        "exit",
        "!",
        f"router bgp {cfg.rr_as}",
        f" bgp router-id {rn.bgp_router_ip}",
        " neighbor VD peer-group",
        f" neighbor VD remote-as {rn.bgp_as}",
        f" neighbor {cfg.rr_ip} remote-as {cfg.rr_as}",
        *([f" neighbor VD password {rn.bgp_password}"] if rn.bgp_password else []),
        *([f" neighbor {cfg.rr_ip} password {cfg.rr_password}"] if getattr(cfg, "rr_password", "") else []),
        f" bgp listen range {lan_net} peer-group VD",
        " !",
        " address-family ipv4 unicast",
        "  neighbor VD route-map PERMIT_IN_EBGP in",
        "  neighbor VD route-map PERMIT_OUT_EBGP out",
        f"  neighbor {cfg.rr_ip} next-hop-self",
        f"  neighbor {cfg.rr_ip} route-map PERMIT_IN_IBGP in",
        " exit-address-family",
        "exit",
        "!",
        f"bgp community-list standard REALNET_PREFIX seq 5 permit {cfg.rr_as}:1",
    ])
    for imported in rn.import_routers or []:
        name = _lab_community_name(imported.get("lab_id") or imported.get("id") or "")
        asn = imported.get("bgp_as")
        if name and asn:
            lines.append(f"bgp community-list standard {name} seq 5 permit {int(asn)}:1")
    lines.extend(["!", "end", ""])
    return "\n".join(lines)


def _lab_community_name(lab_id: str) -> str:
    if not lab_id:
        return ""
    return "LAB_" + hashlib.sha1(str(lab_id).encode("utf-8")).hexdigest() + "_PREFIX"


def _write_frr_config(master: SSHClient, container: str, config: str) -> None:
    script = (
        "cat > /etc/frr/frr.conf <<'EOF'\n"
        + config
        + "EOF\n"
        "if [ -f /etc/frr/daemons ]; then sed -i 's/^bgpd=.*/bgpd=yes/' /etc/frr/daemons; fi\n"
        "chown frr:frr /etc/frr/frr.conf 2>/dev/null || true\n"
        "if [ -x /usr/lib/frr/frrinit.sh ]; then /usr/lib/frr/frrinit.sh restart || /usr/lib/frr/frrinit.sh start || true; fi\n"
        "if command -v vtysh >/dev/null 2>&1; then vtysh -b || true; fi\n"
    )
    master.run(
        f"docker exec {shlex.quote(container)} sh -lc {shlex.quote(script)}",
        check=False,
    )


def destroy_realnets(lab_name: str, clients: dict[str, SSHClient], states: list[RealNetState]) -> None:
    for rn in states:
        master = clients.get("master")
        if master:
            master.run(f"docker rm -f {rn.router_container} 2>/dev/null", check=False)
        for host in rn.hosts:
            client = clients.get(host)
            if not client:
                continue
            bridge = rn.bridge
            vx = naming.realnet_vxlan_iface(lab_name, rn.name)
            veth = naming.realnet_router_veth_name(lab_name, rn.name)
            legacy_veth = f"vh-{rn.name[:10]}"[:15]
            _remove_bridge_forwarding(client, bridge)
            client.run(
                f"ip link del {veth} 2>/dev/null || true; "
                f"legacy_master=$(basename $(readlink /sys/class/net/{legacy_veth}/master 2>/dev/null) "
                f"2>/dev/null || true); "
                f"if [ \"$legacy_master\" = \"{bridge}\" ]; then "
                f"ip link del {legacy_veth} 2>/dev/null || true; "
                f"fi; "
                f"ip link del {vx} 2>/dev/null || true; "
                f"ip link del {bridge} 2>/dev/null || true",
                check=False,
            )
            if host != "master" and master:
                peer = f"{vx}-{host}"[:15]
                master.run(f"ip link del {peer} 2>/dev/null || true", check=False)
