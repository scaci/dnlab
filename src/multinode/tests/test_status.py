"""Tests for the StatusController (M1.4)."""

from __future__ import annotations

from pathlib import Path

from dnlab_multinode.controllers.status import (
    StatusController, _parse_clab_interfaces, _parse_docker_uptime,
)
from dnlab_multinode.controllers.node import (
    NodeLifecycleController, NodeLifecycleError,
)
from dnlab_multinode.models.state import (
    DeploymentState, HostScheduleState, JumphostState, DnsState,
    NodeRuntimeState, RuntimeLinkState, RuntimeRelayState,
)
from dnlab_multinode.services import state as state_svc

from tests.conftest import make_topology


HOSTS_YML = """\
infrastructure:
  master:
    host: 10.0.0.10
    ssh_user: root
    ssh_key: ~/.ssh/id
  workers:
    worker1:
      host: 10.0.0.11
      ssh_user: root
      ssh_key: ~/.ssh/id
    worker2:
      host: 10.0.0.12
      ssh_user: root
      ssh_key: ~/.ssh/id
  underlay_iface: eth0

jumphost:
  image: jh:latest
  host_ip: 192.168.100.1/24

defaults:
  mgmt:
    ipv4_subnet: 172.20.0.0/24
    ipv4_gw: 172.20.0.1
"""


TOPO_YML = """\
name: lab
topology:
  nodes:
    R1:
      kind: linux
      image: alpine
      mgmt-ipv4: 172.20.0.11
    R2:
      kind: linux
      image: alpine
      mgmt-ipv4: 172.20.0.12
  links:
    - endpoints: ["R1:eth1", "R2:eth1"]
"""


def _write_inputs(tmp_path: Path) -> Path:
    hosts = tmp_path / "hosts.yml"
    hosts.write_text(HOSTS_YML)
    topo = tmp_path / "lab.yml"
    topo.write_text(TOPO_YML)
    return topo


def test_status_when_not_deployed(tmp_path):
    topo = _write_inputs(tmp_path)
    ctrl = StatusController(str(topo), hosts_file=str(tmp_path / "hosts.yml"))
    report = ctrl.run()

    assert report.deployed is False
    assert report.lab_name == "lab"
    assert set(report.hosts) == {"master", "worker1", "worker2"}
    assert set(report.nodes) == {"R1", "R2"}
    for ns in report.nodes.values():
        assert ns.state == "missing"


def test_status_unreachable_hosts_marked(tmp_path, monkeypatch):
    topo = _write_inputs(tmp_path)

    # Fabricate a deployment state pinning R1→master, R2→worker1.
    st = DeploymentState(lab_name="lab", topology_file=str(topo), deployed_at="2026-01-01")
    st.runtime_mode = "per-host-apply"
    st.containerlab_versions = {"master": "0.77.0", "worker1": "0.77.0"}
    st.host_apply_status = {"master": "applied", "worker1": "error"}
    st.host_apply_plan = {
        "master": [
            {
                "action": "deploy lab",
                "details": "lab",
                "nodes": [],
            }
        ],
        "worker1": [
            {
                "action": "recreated nodes",
                "details": "R2 (config drift: Env)",
                "nodes": ["R2"],
            }
        ],
    }
    st.reconcile_required = True
    st.scheduling = {
        "master": HostScheduleState(
            host="10.0.0.10", topology_file="lab-master.yml",
            vd=["R1"], resources_used={"cpu": 1, "ram_mb": 512},
        ),
        "worker1": HostScheduleState(
            host="10.0.0.11", topology_file="lab-worker1.yml",
            vd=["R2"], resources_used={"cpu": 1, "ram_mb": 512},
        ),
    }
    st.jumphost = JumphostState(
        node="master", container="jh-lab", mgmt_ip="172.20.0.250",
        host_ip="192.168.100.1/24", ext_network="ext-lab",
    )
    st.dns = DnsState(node="master", container="dns-lab", mgmt_ip="172.20.0.2", entries=3)
    st.runtime_relays = {
        "worker1": RuntimeRelayState(
            host="worker1",
            container="dnlab-lab-runtime-relay",
            bind_ip="10.0.0.11",
            port=23042,
            api_key="secret",
            allowed=["clab-dnlab-lab-R2-R2"],
        ),
    }
    state_svc.save_state(st, Path(topo).parent)

    # All SSH connect attempts fail → hosts must be flagged unreachable.
    def _fake_connect(self):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.connect", _fake_connect
    )

    ctrl = StatusController(str(topo), hosts_file=str(tmp_path / "hosts.yml"))
    report = ctrl.run()

    assert report.deployed is True
    assert report.deployed_at == "2026-01-01"
    assert report.runtime_mode == "per-host-apply"
    assert report.containerlab_versions == {"master": "0.77.0", "worker1": "0.77.0"}
    assert report.host_apply_status == {"master": "applied", "worker1": "error"}
    assert report.host_apply_plan["worker1"][0]["action"] == "recreated nodes"
    assert report.reconcile_required is True
    assert all(not h.reachable for h in report.hosts.values())

    # Scheduling snapshot still surfaces even if hosts are offline.
    assert report.hosts["master"].vd_count == 1
    assert report.hosts["worker1"].cpu_used == 1

    # VDs mapped to unreachable hosts report "unreachable".
    assert report.nodes["R1"].host == "master"
    assert report.nodes["R1"].state == "unreachable"
    assert report.nodes["R2"].host == "worker1"
    assert report.nodes["R2"].state == "unreachable"
    assert report.infra.runtime_relays["worker1"]["container"] == "dnlab-lab-runtime-relay"
    assert report.infra.runtime_relays["worker1"]["allowed"] == 1
    assert report.infra.runtime_relays["worker1"]["running"] is None
    assert report.to_dict()["infra"]["runtime_relays"]["worker1"]["port"] == 23042
    assert report.to_dict()["runtime_mode"] == "per-host-apply"
    assert report.to_dict()["host_apply_status"]["worker1"] == "error"
    assert report.to_dict()["host_apply_plan"]["worker1"][0]["nodes"] == ["R2"]


def _save_state_with_r1_on(topo: Path, host_name: str) -> None:
    st = DeploymentState(lab_name="lab", topology_file=str(topo), deployed_at="2026-01-01")
    st.scheduling = {
        host_name: HostScheduleState(
            host=f"10.0.0.{11 if host_name == 'worker1' else 12}",
            topology_file=f"lab-{host_name}.yml",
            vd=["R1"],
            resources_used={"cpu": 1, "ram_mb": 512},
        ),
    }
    state_svc.save_state(st, Path(topo).parent)


def _fake_reachable_docker(monkeypatch, containers_by_host: dict[str, str]) -> None:
    def _fake_connect(self):
        self._client = object()

    def _fake_close(self):
        self._client = None

    def _fake_run_no_check(self, command, timeout=30):
        if "docker ps -a" in command:
            return 0, containers_by_host.get(self.name, ""), ""
        if "docker inspect" in command:
            return 1, "", ""
        return 0, "", ""

    monkeypatch.setattr("dnlab_multinode.services.ssh.SSHClient.connect", _fake_connect)
    monkeypatch.setattr("dnlab_multinode.services.ssh.SSHClient.close", _fake_close)
    monkeypatch.setattr("dnlab_multinode.services.ssh.SSHClient.run_no_check", _fake_run_no_check)


def test_status_uses_live_host_when_state_is_stale(tmp_path, monkeypatch):
    topo = _write_inputs(tmp_path)
    _save_state_with_r1_on(topo, "worker2")
    _fake_reachable_docker(
        monkeypatch,
        {
            "worker1": "clab-lab-R1\trunning\tUp 3 minutes",
            "worker2": "",
        },
    )

    ctrl = StatusController(str(topo), hosts_file=str(tmp_path / "hosts.yml"))
    report = ctrl.run()

    assert report.nodes["R1"].host == "worker1"
    assert report.nodes["R1"].scheduled_host == "worker2"
    assert report.nodes["R1"].placement_mismatch is True
    assert report.nodes["R1"].state == "running"
    assert report.hosts["worker1"].live_vd_count == 1


def test_status_has_no_mismatch_when_live_matches_state(tmp_path, monkeypatch):
    topo = _write_inputs(tmp_path)
    _save_state_with_r1_on(topo, "worker1")
    _fake_reachable_docker(
        monkeypatch,
        {
            "worker1": "clab-lab-R1\trunning\tUp 3 minutes",
            "worker2": "",
        },
    )

    ctrl = StatusController(str(topo), hosts_file=str(tmp_path / "hosts.yml"))
    report = ctrl.run()

    assert report.nodes["R1"].host == "worker1"
    assert report.nodes["R1"].scheduled_host == "worker1"
    assert report.nodes["R1"].placement_mismatch is False
    assert report.nodes["R1"].state == "running"


def test_status_collects_per_host_containerlab_interfaces(tmp_path, monkeypatch):
    topo = _write_inputs(tmp_path)
    _save_state_with_r1_on(topo, "worker1")
    state = state_svc.load_state("lab", tmp_path)
    state.runtime_mode = "per-host-apply"
    state.node_runtime["R1"].topology_file = "/tmp/dnlab-lab-worker1.clab.yml"
    state.node_runtime["R1"].apply_mode = "live"
    state_svc.save_state(state, tmp_path)
    _fake_reachable_docker(
        monkeypatch,
        {
            "worker1": "clab-lab-R1\trunning\tUp 3 minutes",
            "worker2": "",
        },
    )

    inspected = []

    def _fake_inspect_interfaces(self, topology_file):
        inspected.append((self.name, topology_file))
        return """
        [
          {
            "node_name": "R1",
            "interface_name": "eth1",
            "alias": "ethernet-1/1",
            "state": "up",
            "type": "veth",
            "mac": "aa:bb:cc:dd:ee:ff",
            "mtu": 9500
          }
        ]
        """

    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.inspect_clab_interfaces",
        _fake_inspect_interfaces,
    )

    ctrl = StatusController(str(topo), hosts_file=str(tmp_path / "hosts.yml"))
    report = ctrl.run()

    assert inspected == [("worker1", "/tmp/dnlab-lab-worker1.clab.yml")]
    assert report.nodes["R1"].interfaces == [
        {
            "name": "eth1",
            "alias": "ethernet-1/1",
            "state": "up",
            "type": "veth",
            "mac": "aa:bb:cc:dd:ee:ff",
            "mtu": 9500,
        }
    ]
    assert report.to_dict()["nodes"]["R1"]["interfaces"][0]["name"] == "eth1"
    assert report.nodes["R1"].apply_mode == "live"
    assert report.to_dict()["nodes"]["R1"]["apply_mode"] == "live"


def test_parse_clab_interfaces_accepts_nested_node_maps():
    data = {
        "lab": {
            "R1": [
                {"name": "eth2", "state": "down"},
                {"ifname": "eth1", "oper_state": "up"},
            ],
            "R2": {"interfaces": [{"interface": "eth1", "peer": "R1:eth1"}]},
        }
    }

    assert _parse_clab_interfaces(data, {"R1", "R2"}) == {
        "R1": [
            {"name": "eth1", "state": "up"},
            {"name": "eth2", "state": "down"},
        ],
        "R2": [{"name": "eth1", "peer": "R1:eth1"}],
    }


def test_legacy_runtime_status_still_finds_legacy_container(tmp_path, monkeypatch):
    topo = _write_inputs(tmp_path)
    _save_state_with_r1_on(topo, "worker1")
    commands = []

    def _fake_connect(self):
        self._client = object()

    def _fake_close(self):
        self._client = None

    def _fake_run_no_check(self, command, timeout=30):
        commands.append(command)
        if "docker ps -a" in command:
            return 0, "clab-lab-R1\trunning\tUp 3 minutes", ""
        if "docker inspect" in command:
            return 1, "", ""
        return 0, "", ""

    monkeypatch.setattr("dnlab_multinode.services.ssh.SSHClient.connect", _fake_connect)
    monkeypatch.setattr("dnlab_multinode.services.ssh.SSHClient.close", _fake_close)
    monkeypatch.setattr("dnlab_multinode.services.ssh.SSHClient.run_no_check", _fake_run_no_check)

    ctrl = StatusController(str(topo), hosts_file=str(tmp_path / "hosts.yml"))
    report = ctrl.run()

    assert report.nodes["R1"].state == "running"
    assert report.nodes["R1"].container == "clab-lab-R1"
    assert all("clab-dnlab-lab-" not in command for command in commands)


def test_node_lifecycle_rejects_legacy_per_host_runtime(tmp_path):
    topo = _write_inputs(tmp_path)
    _save_state_with_r1_on(topo, "worker1")

    ctrl = NodeLifecycleController(str(topo), hosts_file=str(tmp_path / "hosts.yml"))

    try:
        ctrl.stop("R1")
    except NodeLifecycleError as exc:
        assert "per-VD runtime" in str(exc)
    else:
        raise AssertionError("legacy runtime stop must be rejected")


def test_node_lifecycle_start_unknown_runtime_node_says_apply_first(tmp_path):
    topo = _write_inputs(tmp_path)
    _save_state_with_r1_on(topo, "worker1")
    state = state_svc.load_state("lab", tmp_path)
    state.runtime_mode = "per-host-apply"
    state.node_runtime.pop("R2", None)
    state_svc.save_state(state, tmp_path)

    ctrl = NodeLifecycleController(str(topo), hosts_file=str(tmp_path / "hosts.yml"))

    try:
        ctrl.start("R2")
    except NodeLifecycleError as exc:
        assert "apply lab changes" in str(exc)
    else:
        raise AssertionError("topology node missing from runtime must require apply")


def test_node_lifecycle_uses_containerlab_stop_for_per_host_runtime(
    tmp_path, monkeypatch,
):
    topo = _write_inputs(tmp_path)
    _save_state_with_r1_on(topo, "worker1")
    state = state_svc.load_state("lab", tmp_path)
    state.runtime_mode = "per-host-apply"
    state.node_runtime["R1"].topology_file = "/tmp/dnlab-lab-worker1.clab.yml"
    state_svc.save_state(state, tmp_path)
    commands = []

    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.connect",
        lambda self: setattr(self, "_client", object()),
    )
    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.close",
        lambda self: setattr(self, "_client", None),
    )

    def fake_lifecycle(self, action, topology_file, node):
        commands.append((self.name, action, topology_file, node))
        return "ok"

    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.lifecycle_clab",
        fake_lifecycle,
    )

    ctrl = NodeLifecycleController(
        str(topo), hosts_file=str(tmp_path / "hosts.yml"),
    )
    result = ctrl.stop("R1")

    assert commands == [
        ("worker1", "stop", "/tmp/dnlab-lab-worker1.clab.yml", "R1"),
    ]
    assert result.node_runtime["R1"].state == "stopped"


def test_node_lifecycle_start_per_host_does_not_trust_stale_running_state(
    tmp_path, monkeypatch,
):
    topo = _write_inputs(tmp_path)
    _save_state_with_r1_on(topo, "worker1")
    state = state_svc.load_state("lab", tmp_path)
    state.runtime_mode = "per-host-apply"
    state.node_runtime["R1"].state = "running"
    state.node_runtime["R1"].topology_file = "/tmp/dnlab-lab-worker1.clab.yml"
    state_svc.save_state(state, tmp_path)
    commands = []

    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.connect",
        lambda self: setattr(self, "_client", object()),
    )
    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.close",
        lambda self: setattr(self, "_client", None),
    )

    def fake_lifecycle(self, action, topology_file, node):
        commands.append((self.name, action, topology_file, node))
        return "ok"

    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.lifecycle_clab",
        fake_lifecycle,
    )
    monkeypatch.setattr(
        NodeLifecycleController, "_wait_container_running",
        staticmethod(lambda client, container, timeout: None),
    )
    monkeypatch.setattr(
        NodeLifecycleController, "_set_default_route",
        lambda self, client, container: None,
    )

    ctrl = NodeLifecycleController(
        str(topo), hosts_file=str(tmp_path / "hosts.yml"),
    )
    result = ctrl.start("R1")

    assert commands == [
        ("worker1", "start", "/tmp/dnlab-lab-worker1.clab.yml", "R1"),
    ]
    assert result.node_runtime["R1"].state == "running"


def test_node_lifecycle_uses_containerlab_restart_for_per_host_runtime(
    tmp_path, monkeypatch,
):
    topo = _write_inputs(tmp_path)
    _save_state_with_r1_on(topo, "worker1")
    state = state_svc.load_state("lab", tmp_path)
    state.runtime_mode = "per-host-apply"
    state.node_runtime["R1"].topology_file = "/tmp/dnlab-lab-worker1.clab.yml"
    state_svc.save_state(state, tmp_path)
    commands = []

    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.connect",
        lambda self: setattr(self, "_client", object()),
    )
    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.close",
        lambda self: setattr(self, "_client", None),
    )

    def fake_lifecycle(self, action, topology_file, node):
        commands.append((self.name, action, topology_file, node))
        return "ok"

    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.lifecycle_clab",
        fake_lifecycle,
    )
    monkeypatch.setattr(
        NodeLifecycleController, "_wait_container_running",
        staticmethod(lambda client, container, timeout: None),
    )
    monkeypatch.setattr(
        NodeLifecycleController, "_set_default_route",
        lambda self, client, container: None,
    )

    ctrl = NodeLifecycleController(
        str(topo), hosts_file=str(tmp_path / "hosts.yml"),
    )
    result = ctrl.restart("R1")

    assert commands == [
        ("worker1", "restart", "/tmp/dnlab-lab-worker1.clab.yml", "R1"),
    ]
    assert result.node_runtime["R1"].state == "running"


def test_node_lifecycle_reconcile_per_host_recreates_dnlab_owned_links(
    tmp_path, monkeypatch,
):
    topo = _write_inputs(tmp_path)
    st = DeploymentState(
        lab_name="lab",
        topology_file=str(topo),
        deployed_at="2026-01-01",
        runtime_mode="per-host-apply",
    )
    st.scheduling = {
        "master": HostScheduleState(
            host="10.0.0.10",
            topology_file="/tmp/dnlab-lab-master.clab.yml",
            vd=["R1"],
        ),
        "worker1": HostScheduleState(
            host="10.0.0.11",
            topology_file="/tmp/dnlab-lab-worker1.clab.yml",
            vd=["R2"],
        ),
    }
    st.node_runtime = {
        "R1": NodeRuntimeState(
            node="R1",
            state="running",
            host="master",
            container="clab-lab-R1",
            topology_file="/tmp/dnlab-lab-master.clab.yml",
        ),
        "R2": NodeRuntimeState(
            node="R2",
            state="running",
            host="worker1",
            container="clab-lab-R2",
            topology_file="/tmp/dnlab-lab-worker1.clab.yml",
        ),
    }
    st.runtime_links = [
        RuntimeLinkState(
            id="same",
            link_type="same_host",
            endpoint_a={"node": "R1", "iface": "eth1"},
            endpoint_b={"node": "R3", "iface": "eth1"},
            host_a="master",
            host_b="master",
        ),
        RuntimeLinkState(
            id="cross",
            link_type="cross_host",
            endpoint_a={"node": "R1", "iface": "eth2"},
            endpoint_b={"node": "R2", "iface": "eth2"},
            host_a="master",
            host_b="worker1",
            host_endpoint_a="e1",
            host_endpoint_b="e2",
            vxlan_id=1001,
        ),
        RuntimeLinkState(
            id="realnet",
            link_type="real_net",
            endpoint_a={"node": "R1", "iface": "eth3"},
            endpoint_b={"real_net": "wan"},
            host_a="master",
            host_b="master",
        ),
    ]
    state_svc.save_state(st, tmp_path)

    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.connect",
        lambda self: setattr(self, "_client", object()),
    )
    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.SSHClient.close",
        lambda self: setattr(self, "_client", None),
    )
    created = []

    def fake_create_link(
        link, clients, underlay_ips=None, running_nodes=None, container_names=None,
    ):
        created.append((link.id, link.link_type, dict(underlay_ips or {}), set(running_nodes or set())))
        link.state = "up"
        return link

    monkeypatch.setattr(
        "dnlab_multinode.controllers.node.runtime_links_svc.create_link",
        fake_create_link,
    )

    ctrl = NodeLifecycleController(
        str(topo), hosts_file=str(tmp_path / "hosts.yml"),
    )
    result = ctrl.reconcile("R1")

    assert created == [
        (
            "cross",
            "cross_host",
            {"master": "10.0.0.10", "worker1": "10.0.0.11"},
            {"R1", "R2"},
        ),
        (
            "realnet",
            "real_net",
            {"master": "10.0.0.10", "worker1": "10.0.0.11"},
            {"R1", "R2"},
        ),
    ]
    assert result.runtime_links[1].state == "up"


def test_status_keeps_missing_when_container_absent_everywhere(tmp_path, monkeypatch):
    topo = _write_inputs(tmp_path)
    _save_state_with_r1_on(topo, "worker1")
    _fake_reachable_docker(monkeypatch, {"worker1": "", "worker2": ""})

    ctrl = StatusController(str(topo), hosts_file=str(tmp_path / "hosts.yml"))
    report = ctrl.run()

    assert report.nodes["R1"].host == "worker1"
    assert report.nodes["R1"].scheduled_host == "worker1"
    assert report.nodes["R1"].placement_mismatch is False
    assert report.nodes["R1"].state == "missing"


def test_status_reports_duplicate_container_hosts(tmp_path, monkeypatch):
    topo = _write_inputs(tmp_path)
    _save_state_with_r1_on(topo, "worker2")
    _fake_reachable_docker(
        monkeypatch,
        {
            "worker1": "clab-lab-R1\trunning\tUp 3 minutes",
            "worker2": "clab-lab-R1\trunning\tUp 2 minutes",
        },
    )

    ctrl = StatusController(str(topo), hosts_file=str(tmp_path / "hosts.yml"))
    report = ctrl.run()

    assert report.nodes["R1"].host == "worker2"
    assert report.nodes["R1"].duplicate_hosts == ["worker1", "worker2"]
    assert report.nodes["R1"].placement_mismatch is True
    assert report.nodes["R1"].state == "running"


def test_status_report_to_dict_is_json_safe(tmp_path):
    import json
    topo = _write_inputs(tmp_path)
    ctrl = StatusController(str(topo), hosts_file=str(tmp_path / "hosts.yml"))
    report = ctrl.run()
    blob = json.dumps(report.to_dict())
    assert '"lab_name": "lab"' in blob
    assert '"deployed": false' in blob


def test_parse_docker_uptime():
    assert _parse_docker_uptime("Up 5 seconds") == 5
    assert _parse_docker_uptime("Up 3 minutes") == 180
    assert _parse_docker_uptime("Up 2 hours") == 7200
    assert _parse_docker_uptime("Up About 1 hour") == 3600
    assert _parse_docker_uptime("Exited (0) 3 minutes ago") == 0
