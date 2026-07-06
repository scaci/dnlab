"""Tests for per-VD runtime link reconciliation."""

from dnlab_multinode.models.schedule import SchedulePlan, HostAssignment, CrossHostLink
from dnlab_multinode.models.state import RuntimeLinkState
from dnlab_multinode.models.topology import VDNode, Link, RealNet, RealNetLink
from dnlab_multinode.services import runtime_links


class FakeClient:
    def __init__(self):
        self.commands = []

    def run(self, cmd, check=True):
        self.commands.append((cmd, check))
        return ""


def test_build_runtime_links_same_cross_and_realnet(topo_factory):
    nodes = {
        "R1": VDNode(name="R1", kind="linux", image="x"),
        "R2": VDNode(name="R2", kind="linux", image="x"),
        "R3": VDNode(name="R3", kind="linux", image="x"),
    }
    local = Link("R1", "eth1", "R2", "eth1")
    topo = topo_factory(nodes=nodes, links=[local], num_workers=1)
    topo.real_nets = {"access": RealNet(name="access", ipv4="192.0.2.1/24")}
    topo.real_net_links = [
        RealNetLink(
            real_net="access", node="R3", iface="eth1",
            host="worker1", bridge_iface="rn-R3-e1",
        )
    ]
    cross = CrossHostLink(
        vxlan_id=3001,
        source_node="R2", source_iface="eth2",
        target_node="R3", target_iface="eth2",
        source_host="master", target_host="worker1",
        source_host_iface="R2-e2-vx",
        target_host_iface="R3-e2-vx",
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.1", vd_names=["R1", "R2"]),
            "worker1": HostAssignment("worker1", "10.0.0.2", vd_names=["R3"]),
        },
        local_links=[local],
        cross_host_links=[cross],
    )

    links = runtime_links.build_runtime_links(topo, plan)

    assert [link.link_type for link in links] == ["same_host", "cross_host", "real_net"]
    assert links[0].host_a == "master"
    assert links[1].host_endpoint_a == "R2-e2-vx"
    assert links[1].vxlan_id == 3001
    assert links[2].host_endpoint_b.startswith("br")


def test_create_and_delete_same_host_link():
    client = FakeClient()
    link = RuntimeLinkState(
        id="l0",
        link_type="same_host",
        endpoint_a={"node": "R1", "iface": "eth1"},
        endpoint_b={"node": "R2", "iface": "eth1"},
        host_a="master",
        host_b="master",
        host_endpoint_a="rt-R1-e1-0",
        host_endpoint_b="rt-R2-e1-0",
    )

    runtime_links.create_link(link, {"master": client}, running_nodes={"R1", "R2"})
    runtime_links.delete_link(link, {"master": client})

    commands = [cmd for cmd, _ in client.commands]
    assert any("type bridge" in cmd for cmd in commands)
    assert any("iptables -C FORWARD -i br-rt-" in cmd for cmd in commands)
    assert any("iptables -C FORWARD -o br-rt-" in cmd for cmd in commands)
    assert any("master br-rt-" in cmd for cmd in commands)
    assert any("iptables -D FORWARD -i br-rt-" in cmd for cmd in commands)
    assert any("iptables -D FORWARD -o br-rt-" in cmd for cmd in commands)
    assert any("ip link delete br-rt-" in cmd for cmd in commands)


def test_cross_host_link_uses_containerlab_vxlan_create_and_delete():
    left = FakeClient()
    right = FakeClient()
    link = RuntimeLinkState(
        id="vx0",
        link_type="cross_host",
        endpoint_a={"node": "R1", "iface": "eth1"},
        endpoint_b={"node": "R2", "iface": "eth1"},
        host_a="worker1",
        host_b="worker2",
        host_endpoint_a="R1-e1-vx",
        host_endpoint_b="R2-e1-vx",
        vxlan_id=3499,
    )

    runtime_links.create_link(
        link,
        {"worker1": left, "worker2": right},
        underlay_ips={"worker1": "10.0.0.1", "worker2": "10.0.0.2"},
        running_nodes={"R1", "R2"},
    )
    runtime_links.delete_link(link, {"worker1": left, "worker2": right})

    assert left.commands[0][0] == (
        "ip link show vx-R1-e1-vx >/dev/null 2>&1 || "
        "containerlab tools vxlan create --remote 10.0.0.2 "
        "--id 3499 --link R1-e1-vx"
    )
    assert right.commands[0][0] == (
        "ip link show vx-R2-e1-vx >/dev/null 2>&1 || "
        "containerlab tools vxlan create --remote 10.0.0.1 "
        "--id 3499 --link R2-e1-vx"
    )
    assert any("ip link delete vx-R1-e1-vx" in cmd for cmd, _ in left.commands)
    assert any("ip link delete vx-R2-e1-vx" in cmd for cmd, _ in right.commands)


def test_cross_host_link_keeps_full_containerlab_vxlan_altname():
    left = FakeClient()
    right = FakeClient()
    link = RuntimeLinkState(
        id="vx0",
        link_type="cross_host",
        endpoint_a={"node": "lab1", "iface": "eth2"},
        endpoint_b={"node": "host1", "iface": "eth1"},
        host_a="worker1",
        host_b="worker2",
        host_endpoint_a="vx9e3-lab1-e2",
        host_endpoint_b="vx9e3-host-e1",
        vxlan_id=3235,
    )

    runtime_links.create_link(
        link,
        {"worker1": left, "worker2": right},
        underlay_ips={"worker1": "10.0.0.1", "worker2": "10.0.0.2"},
        running_nodes={"lab1", "host1"},
    )
    runtime_links.delete_link(link, {"worker1": left, "worker2": right})

    assert left.commands[0][0].startswith("ip link show vx-vx9e3-lab1-e2 ")
    assert right.commands[0][0].startswith("ip link show vx-vx9e3-host-e1 ")
    assert any("ip link delete vx-vx9e3-lab1-e2" in cmd for cmd, _ in left.commands)
    assert any("ip link delete vx-vx9e3-host-e1" in cmd for cmd, _ in right.commands)


def test_create_link_marks_partial_if_peer_stopped():
    client = FakeClient()
    link = RuntimeLinkState(
        id="l0",
        link_type="same_host",
        endpoint_a={"node": "R1", "iface": "eth1"},
        endpoint_b={"node": "R2", "iface": "eth1"},
        host_a="master",
        host_b="master",
    )

    runtime_links.create_link(link, {"master": client}, running_nodes={"R1"})

    assert link.state == "partial"
    assert "R2" in link.last_error
    assert client.commands == []
