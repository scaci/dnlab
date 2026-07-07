import yaml

from dnlab_multinode.services.config import parse_topology
from dnlab_multinode.services.scheduler import compute_schedule
from dnlab_multinode.models.schedule import VDResources, HostResources


def test_parse_topology_extracts_realnet_pseudonode(tmp_path):
    hosts = tmp_path / "hosts.yml"
    hosts.write_text("""
infrastructure:
  master:
    host: 10.0.0.10
    ssh_user: root
    ssh_key: ~/.ssh/id
  workers: {}
  underlay_iface: eth0
""")
    topo_file = tmp_path / "topo.yml"
    topo_file.write_text(yaml.safe_dump({
        "name": "lab",
        "topology": {
            "nodes": {
                "r1": {"kind": "linux", "image": "alpine"},
                "real_net1": {
                    "kind": "_real_net",
                    "image": "",
                    "extra": {
                        "ipv4": "192.168.50.1/24",
                        "ospf": True,
                        "nat": False,
                    },
                },
            },
            "links": [
                {"endpoints": ["r1:eth1", "real_net1:real"]},
            ],
        },
    }))

    topo = parse_topology(topo_file, hosts_file=hosts)

    assert "r1" in topo.nodes
    assert "real_net1" not in topo.nodes
    assert topo.real_nets["real_net1"].bgp is True
    assert topo.real_net_links[0].node == "r1"


def test_scheduler_assigns_realnet_link_host(topo_factory):
    from dnlab_multinode.models.topology import RealNet, RealNetLink

    topo = topo_factory(num_workers=0)
    topo.real_nets = {
        "real_net1": RealNet(name="real_net1", ipv4="192.168.50.1/24")
    }
    topo.real_net_links = [
        RealNetLink(real_net="real_net1", node="R1", iface="eth1")
    ]
    vd = {
        name: VDResources(name=name, image=node.image, cpu=1, ram_mb=64)
        for name, node in topo.nodes.items()
    }
    hosts = {
        "master": HostResources(
            name="master", host="10.0.0.10",
            cpu_available=8, ram_mb_available=8192, is_master=True,
        )
    }

    plan = compute_schedule(topo, vd, hosts)

    assert plan.host_for_vd("R1") == "master"
    assert topo.real_net_links[0].host == "master"
    assert topo.real_net_links[0].bridge_iface


class FakeSSH:
    def __init__(self):
        self.commands = []

    def run(self, cmd, check=True):
        self.commands.append(cmd)
        return ""

    def run_no_check(self, cmd):
        self.commands.append(cmd)
        return 0, "", ""


def test_realnet_setup_orders_bridges_before_vxlan_and_adds_forwarding(topo_factory):
    from dnlab_multinode.models.topology import RealNet, RealNetLink
    from dnlab_multinode.services import realnet

    topo = topo_factory(num_workers=1)
    topo.real_nets = {
        "real_net1": RealNet(name="real_net1", ipv4="192.168.50.1/24")
    }
    topo.real_net_links = [
        RealNetLink(real_net="real_net1", node="R1", iface="eth1", host="worker1")
    ]
    clients = {"master": FakeSSH(), "worker1": FakeSSH()}

    realnet.setup_bridges(
        topo, clients, {"master": "10.0.0.10", "worker1": "10.0.0.11"}
    )

    master_cmds = clients["master"].commands
    bridge_idx = next(i for i, c in enumerate(master_cmds) if "type bridge" in c)
    peer_idx = next(i for i, c in enumerate(master_cmds) if "type vxlan" in c)

    assert bridge_idx < peer_idx
    assert any("iptables -C FORWARD -i brlabrealn" in c for c in master_cmds)
    assert any("iptables -C FORWARD -o brlabrealn" in c for c in master_cmds)


def test_realnet_destroy_removes_forwarding_rules():
    from dnlab_multinode.models.state import RealNetState
    from dnlab_multinode.services import realnet

    clients = {"master": FakeSSH(), "worker1": FakeSSH()}
    states = [
        RealNetState(
            name="real_net1",
            bridge="brlabreal",
            vxlan_id=5001,
            hosts=["master", "worker1"],
            router_container="dnlab-lab-real_net1-realnet",
        )
    ]

    realnet.destroy_realnets("lab", clients, states)

    assert any("iptables -D FORWARD -i brlabreal" in c for c in clients["master"].commands)
    assert any("iptables -D FORWARD -o brlabreal" in c for c in clients["master"].commands)
    assert any("iptables -D FORWARD -i brlabreal" in c for c in clients["worker1"].commands)
    assert any("iptables -C FORWARD -i brlabreal -j ACCEPT" in c for c in clients["master"].commands)
    assert any("iptables -C FORWARD -o brlabreal -j ACCEPT" in c for c in clients["worker1"].commands)
    assert any("--comment 'set by containerlab'" in c for c in clients["master"].commands)


def test_realnet_bgp_writes_frr_config(topo_factory):
    from dnlab_multinode.models.state import RealNetState
    from dnlab_multinode.models.topology import RealNet
    from dnlab_multinode.services import realnet

    class DeploySSH(FakeSSH):
        def run_no_check(self, cmd):
            self.commands.append(cmd)
            if "docker network inspect" in cmd:
                return 1, "", ""
            return 0, "", ""

        def run(self, cmd, check=True):
            self.commands.append(cmd)
            if ".State.Pid" in cmd:
                return "1234"
            if ".NetworkSettings.Networks" in cmd:
                return "192.168.101.2"
            return ""

    topo = topo_factory(num_workers=0)
    rn = RealNet(
        name="real_net1",
        ipv4="192.168.50.1/24",
        bgp=True,
        bgp_as=64513,
        bgp_router_ip="10.0.0.21",
        bgp_password="router-secret",
    )
    topo.realnet_infra.rr_as = 64512
    topo.realnet_infra.rr_ip = "10.0.0.10"
    topo.realnet_infra.host_net = "10.0.0.0/24"
    topo.realnet_infra.wan_iface = "br-host"
    topo.realnet_infra.rr_password = "rr-secret"
    state = RealNetState(
        name="real_net1",
        bridge="brlabreal",
        vxlan_id=5001,
        hosts=["master"],
        router_container="dnlab-lab-real_net1-realnet",
        lan_ipv4=rn.ipv4,
        bgp=True,
        bgp_as=64513,
        bgp_router_ip="10.0.0.21",
    )

    ssh = DeploySSH()
    deployed = realnet.deploy_router(topo, rn, state, ssh)

    assert deployed.router_wan_ip == "10.0.0.21"
    assert any("--network dnlab-realnet-bgp" in cmd and "--ip 10.0.0.21" in cmd for cmd in ssh.commands)
    assert any("/etc/frr/frr.conf" in cmd and "router bgp 64512" in cmd for cmd in ssh.commands)
    assert any("neighbor 10.0.0.10 password rr-secret" in cmd for cmd in ssh.commands)
    assert any("neighbor VD password router-secret" in cmd for cmd in ssh.commands)


def _deploy_rr_for_image_test(topo_factory):
    from dnlab_multinode.services import realnet

    class DeploySSH(FakeSSH):
        def run_no_check(self, cmd):
            self.commands.append(cmd)
            if "docker network inspect" in cmd:
                return 1, "", ""
            return 0, "", ""

        def run(self, cmd, check=True):
            self.commands.append(cmd)
            if ".State.Pid" in cmd:
                return "1234"
            return ""

    topo = topo_factory(num_workers=0)
    topo.realnet_infra.rr_ip = "10.0.0.10"
    topo.realnet_infra.host_net = "10.0.0.0/24"
    topo.realnet_infra.wan_iface = "br-host"

    ssh = DeploySSH()
    realnet.deploy_route_reflector(topo, ssh)
    return ssh.commands


def test_realnet_rr_defaults_to_latest_image(monkeypatch, topo_factory):
    monkeypatch.delenv("DNLAB_VERSION", raising=False)
    monkeypatch.delenv("DNLAB_RUNTIME_IMAGE_PREFIX", raising=False)

    commands = _deploy_rr_for_image_test(topo_factory)

    assert any("docker image inspect dnlab-realnet-rr:latest" in cmd for cmd in commands)
    assert any(cmd.endswith("dnlab-realnet-rr:latest") for cmd in commands)


def test_realnet_rr_uses_release_image(monkeypatch, topo_factory):
    monkeypatch.setenv("DNLAB_VERSION", "0.1.0")
    monkeypatch.setenv("DNLAB_RUNTIME_IMAGE_PREFIX", "dnlab-")

    commands = _deploy_rr_for_image_test(topo_factory)

    assert any("docker image inspect dnlab-realnet-rr:0.1.0" in cmd for cmd in commands)
    assert any(cmd.endswith("dnlab-realnet-rr:0.1.0") for cmd in commands)
    assert not any("dnlab-realnet-rr:latest" in cmd for cmd in commands)


def test_realnet_router_veth_name_is_unique_per_lab():
    from dnlab_multinode.utils import naming

    first = naming.realnet_router_veth_name("1a1fc20b0f7e", "net1")
    second = naming.realnet_router_veth_name("5f9eb562b4ce", "net1")

    assert first == "vh1a1fcnet1"
    assert second == "vh5f9ebnet1"
    assert first != second
    assert len(first) <= 15
    assert len(second) <= 15


def test_realnet_lifecycle_reconcile_redeploys_router(monkeypatch, topo_factory):
    from dnlab_multinode.controllers import realnet as realnet_ctrl
    from dnlab_multinode.models.state import DeploymentState, RealNetState
    from dnlab_multinode.models.topology import RealNet

    topo = topo_factory(num_workers=0)
    topo.real_nets = {
        "net1": RealNet(
            name="net1",
            ipv4="100.64.1.1/24",
            nat=False,
            bgp=True,
            bgp_as=64513,
            bgp_router_ip="192.168.10.2",
            bgp_password="router-secret",
        )
    }
    topo.realnet_infra.rr_ip = "192.168.10.1"
    topo.realnet_infra.host_net = "192.168.10.0/24"
    state = DeploymentState(lab_name="lab", topology_file="/tmp/lab.yml")
    state.realnets = [
        RealNetState(
            name="net1",
            bridge="brlabnet1",
            vxlan_id=5001,
            hosts=["master"],
            router_container="dnlab-lab-net1-realnet",
            nat=True,
            bgp=False,
        )
    ]
    calls = []

    class FakeMaster:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def connect(self):
            calls.append(("connect", self.kwargs["host"]))

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(realnet_ctrl, "parse_topology", lambda *a, **k: topo)
    monkeypatch.setattr(realnet_ctrl, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(realnet_ctrl, "save_state", lambda saved, directory: calls.append(("save", saved.lab_name)))
    monkeypatch.setattr(realnet_ctrl, "SSHClient", FakeMaster)
    monkeypatch.setattr(realnet_ctrl.realnet_svc, "deploy_route_reflector", lambda t, c: calls.append(("rr", t.name)))
    monkeypatch.setattr(realnet_ctrl.realnet_svc, "deploy_router", lambda t, rn, rn_state, c: calls.append(("router", rn.name, rn_state.bgp)))

    out = realnet_ctrl.RealNetLifecycleController("/tmp/lab.yml").reconcile("net1")

    assert out is state
    assert ("rr", "lab") in calls
    assert ("router", "net1", True) in calls
    assert ("save", "lab") in calls
    assert state.realnets[0].nat is False
    assert state.realnets[0].bgp is True
    assert state.realnets[0].bgp_as == 64513
