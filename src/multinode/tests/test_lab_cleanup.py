"""Tests for the conservative lab-cleanup reconciler."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from dnlab_multinode import cli as cli_mod
from dnlab_multinode.models.state import (
    DeploymentState,
    DnsState,
    HostScheduleState,
    JumphostState,
    MgmtAnchorState,
    MgmtState,
    NodeRuntimeState,
    RuntimeRelayState,
    VxlanLinkState,
)
from dnlab_multinode.models.topology import InfraHost
from dnlab_multinode.services import lab_cleanup as cleanup
from dnlab_multinode.services import state as state_svc
from dnlab_multinode.services.hosts_config import (
    HostsConfig,
    ImageSyncConfig,
    LabCleanupConfig,
    MgmtDefaults,
)


class FakeClient:
    def __init__(
        self,
        name: str,
        *,
        containers: str = "",
        networks: list[str] | None = None,
        network_counts: dict[str, int] | None = None,
        interfaces: list[str] | None = None,
        fail_connect: bool = False,
    ):
        self.name = name
        self.host = f"10.0.0.{10 if name == 'master' else 11}"
        self.containers = containers
        self.networks = networks or []
        self.network_counts = network_counts or {}
        self.interfaces = interfaces or []
        self.fail_connect = fail_connect
        self.connected = False
        self.commands: list[str] = []

    def connect(self):
        if self.fail_connect:
            raise RuntimeError("connection refused")
        self.connected = True

    def close(self):
        self.connected = False

    def run_no_check(self, cmd, timeout=30):
        self.commands.append(cmd)
        if "docker ps -a" in cmd:
            return 0, self.containers, ""
        if "docker network ls" in cmd:
            return 0, "\n".join(self.networks), ""
        if "docker network inspect" in cmd:
            name = cmd.rsplit(" ", 1)[-1]
            if name in self.network_counts:
                return 0, str(self.network_counts[name]), ""
            return 1, "", "missing"
        if "ip -o link show" in cmd:
            return 0, "\n".join(self.interfaces), ""
        if cmd.startswith("docker rm") or cmd.startswith("docker network rm") or cmd.startswith("ip link delete"):
            return 0, "", ""
        if cmd.startswith("[ -f /var/run/dnsmasq-"):
            return 0, "", ""
        return 0, "", ""


def _hosts() -> HostsConfig:
    return HostsConfig(
        master=InfraHost(name="master", host="10.0.0.10", ssh_user="root", ssh_key="~/.ssh/id", is_master=True),
        workers={
            "worker1": InfraHost(name="worker1", host="10.0.0.11", ssh_user="root", ssh_key="~/.ssh/id"),
        },
        underlay_iface="eth0",
        mgmt_defaults=MgmtDefaults(),
        image_sync=ImageSyncConfig(),
        lab_cleanup=LabCleanupConfig(grace_seconds=600),
    )


def _state(tmp_path: Path) -> DeploymentState:
    st = DeploymentState(lab_name="demo", topology_file=str(tmp_path / "demo.yml"))
    st.scheduling = {
        "master": HostScheduleState(host="10.0.0.10", topology_file="/tmp/demo-master.yml", vd=["R1"]),
        "worker1": HostScheduleState(host="10.0.0.11", topology_file="/tmp/demo-worker.yml", vd=["R2"]),
    }
    st.node_runtime = {
        "R1": NodeRuntimeState(node="R1", host="master", container="clab-dnlab-demo-R1-R1", topology_file="/tmp/r1.yml"),
        "R2": NodeRuntimeState(node="R2", host="worker1", container="clab-dnlab-demo-R2-R2", topology_file="/tmp/r2.yml"),
    }
    st.mgmt = MgmtState(
        subnet="172.20.0.0/24",
        gateway="172.20.0.1",
        bridge="br-demo",
        vrf="vrf-demo",
        vxlan_id=1000,
        vxlan_iface="vx-demo-mgmt",
    )
    st.vxlan_dataplane = [
        VxlanLinkState(
            id=2000,
            link="R1:eth1 <-> R2:eth1",
            side_a={"node": "master", "iface": "vxabc-R1-e1"},
            side_b={"node": "worker1", "iface": "vxabc-R2-e1"},
        )
    ]
    state_svc.save_state(st, tmp_path)
    return st


def test_parse_artifact_lab_known_patterns():
    known = {"demo"}
    assert cleanup.parse_artifact_lab("container", "clab-dnlab-demo-R1-R1", known) == "demo"
    assert cleanup.parse_artifact_lab("container", "clab-dnlab-4653dc372b28-n9kv1-n9kv1", set()) == "4653dc372b28"
    assert cleanup.parse_artifact_lab("container", "clab-dnlab-demo-site-vjunos-node-vjunos-node", set()) == "demo-site"
    assert cleanup.parse_artifact_lab("container", "clab-demo-R1", known) == "demo"
    assert cleanup.parse_artifact_lab("container", "dnlab-demo-dns", known) == "demo"
    assert cleanup.parse_artifact_lab("container", "dnlab-demo-jumphost", known) == "demo"
    assert cleanup.parse_artifact_lab("container", "dnlab-demo-runtime-relay", known) == "demo"
    assert cleanup.parse_artifact_lab("container", "clab-dnlab-demo-mgmt-worker1-mgmt-anchor", known) == "demo"
    assert cleanup.parse_artifact_lab("container", "dnlab-demo-wan-realnet", known) == "demo"
    assert cleanup.parse_artifact_lab("network", "dnlab-jumphost", known) == ""


def test_running_container_protects_entire_lab():
    inv = {
        "master": cleanup.HostCleanupInventory(
            name="master",
            host="10.0.0.10",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact("container", "clab-dnlab-demo-R1-R1", "master", "demo", state="running"),
                cleanup.CleanupArtifact("network", "mgmt-demo", "master", "demo", metadata={"containers": 0}),
            ],
        )
    }
    plans = cleanup.build_cleanup_plan(inv, {}, grace_seconds=0)
    assert plans["demo"].protected is True
    assert "lab-runtime-running" in plans["demo"].reasons
    assert plans["demo"].actions == []


def test_running_runtime_relay_without_running_vd_is_cleaned():
    inv = {
        "worker1": cleanup.HostCleanupInventory(
            name="worker1",
            host="10.0.0.11",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact(
                    "container",
                    "dnlab-demo-runtime-relay",
                    "worker1",
                    "demo",
                    state="running",
                ),
            ],
        )
    }

    plans = cleanup.build_cleanup_plan(inv, {}, grace_seconds=0)
    commands = [a.command for a in plans["demo"].actions]

    assert plans["demo"].protected is False
    assert commands == ["docker rm -f dnlab-demo-runtime-relay"]


def test_running_vd_protects_lab_with_running_runtime_relay():
    inv = {
        "worker1": cleanup.HostCleanupInventory(
            name="worker1",
            host="10.0.0.11",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact(
                    "container",
                    "clab-dnlab-demo-R1-R1",
                    "worker1",
                    "demo",
                    state="running",
                ),
                cleanup.CleanupArtifact(
                    "container",
                    "dnlab-demo-runtime-relay",
                    "worker1",
                    "demo",
                    state="running",
                ),
            ],
        )
    }

    plans = cleanup.build_cleanup_plan(inv, {}, grace_seconds=0)

    assert plans["demo"].protected is True
    assert "lab-runtime-running" in plans["demo"].reasons
    assert plans["demo"].actions == []


def test_running_legacy_vd_from_state_scheduling_protects_lab(tmp_path):
    st = DeploymentState(lab_name="demo", topology_file=str(tmp_path / "demo.yml"))
    st.scheduling = {
        "worker1": HostScheduleState(
            host="10.0.0.11",
            topology_file="/tmp/demo-worker.yml",
            vd=["R1"],
        ),
    }
    inv = {
        "worker1": cleanup.HostCleanupInventory(
            name="worker1",
            host="10.0.0.11",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact(
                    "container",
                    "clab-demo-R1",
                    "worker1",
                    "demo",
                    state="running",
                ),
                cleanup.CleanupArtifact(
                    "container",
                    "dnlab-demo-runtime-relay",
                    "worker1",
                    "demo",
                    state="running",
                ),
            ],
        )
    }

    plans = cleanup.build_cleanup_plan(inv, {"demo": st}, grace_seconds=0)

    assert plans["demo"].protected is True
    assert "lab-runtime-running" in plans["demo"].reasons
    assert plans["demo"].actions == []


def test_running_service_containers_without_running_vd_are_cleaned():
    inv = {
        "master": cleanup.HostCleanupInventory(
            name="master",
            host="10.0.0.10",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact("container", "dnlab-demo-dns", "master", "demo", state="running"),
                cleanup.CleanupArtifact("container", "dnlab-demo-jumphost", "master", "demo", state="running"),
            ],
        ),
        "worker1": cleanup.HostCleanupInventory(
            name="worker1",
            host="10.0.0.11",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact(
                    "container",
                    "dnlab-demo-runtime-relay",
                    "worker1",
                    "demo",
                    state="running",
                ),
            ],
        ),
    }

    plans = cleanup.build_cleanup_plan(inv, {}, grace_seconds=0)
    commands = {a.command for a in plans["demo"].actions}

    assert plans["demo"].protected is False
    assert commands == {
        "docker rm -f dnlab-demo-dns",
        "docker rm -f dnlab-demo-jumphost",
        "docker rm -f dnlab-demo-runtime-relay",
    }


def test_state_derived_running_service_containers_without_running_vd_are_cleaned(tmp_path):
    st = DeploymentState(lab_name="demo", topology_file=str(tmp_path / "demo.yml"))
    st.dns = DnsState(node="master", container="dnlab-demo-dns", mgmt_ip="172.20.0.253")
    st.jumphost = JumphostState(
        node="master",
        container="dnlab-demo-jumphost",
        mgmt_ip="172.20.0.254",
        host_ip="192.168.100.2",
        ext_network="dnlab-jumphost",
    )
    st.runtime_relays = {
        "worker1": RuntimeRelayState(
            host="worker1",
            container="dnlab-demo-runtime-relay",
            bind_ip="10.0.0.11",
            port=23042,
        ),
    }
    inv = {
        "master": cleanup.HostCleanupInventory(
            name="master",
            host="10.0.0.10",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact("container", "dnlab-demo-dns", "master", "demo", state="running"),
                cleanup.CleanupArtifact("container", "dnlab-demo-jumphost", "master", "demo", state="running"),
            ],
        ),
        "worker1": cleanup.HostCleanupInventory(
            name="worker1",
            host="10.0.0.11",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact(
                    "container",
                    "dnlab-demo-runtime-relay",
                    "worker1",
                    "demo",
                    state="running",
                ),
            ],
        ),
    }

    plans = cleanup.build_cleanup_plan(inv, {"demo": st}, grace_seconds=0)
    commands = {a.command for a in plans["demo"].actions}

    assert plans["demo"].protected is False
    assert commands == {
        "docker rm -f dnlab-demo-dns",
        "docker rm -f dnlab-demo-jumphost",
        "docker rm -f dnlab-demo-runtime-relay",
    }


def test_running_mgmt_anchor_does_not_protect_stopped_lab():
    inv = {
        "master": cleanup.HostCleanupInventory(
            name="master",
            host="10.0.0.10",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact("container", "clab-dnlab-demo-R1-R1", "master", "demo", state="exited"),
                cleanup.CleanupArtifact(
                    "container",
                    "clab-dnlab-demo-mgmt-master-mgmt-anchor",
                    "master",
                    "demo",
                    state="running",
                ),
            ],
        )
    }

    plans = cleanup.build_cleanup_plan(inv, {}, grace_seconds=0)
    commands = [a.command for a in plans["demo"].actions]

    assert plans["demo"].protected is False
    assert "docker rm -f clab-dnlab-demo-R1-R1" in commands
    assert "docker rm -f clab-dnlab-demo-mgmt-master-mgmt-anchor" in commands


def test_running_vd_still_protects_lab_with_running_mgmt_anchor():
    inv = {
        "master": cleanup.HostCleanupInventory(
            name="master",
            host="10.0.0.10",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact("container", "clab-dnlab-demo-R1-R1", "master", "demo", state="running"),
                cleanup.CleanupArtifact(
                    "container",
                    "clab-dnlab-demo-mgmt-master-mgmt-anchor",
                    "master",
                    "demo",
                    state="running",
                ),
            ],
        )
    }

    plans = cleanup.build_cleanup_plan(inv, {}, grace_seconds=0)

    assert plans["demo"].protected is True
    assert plans["demo"].actions == []


def test_state_derived_running_mgmt_anchor_is_cleaned(tmp_path):
    st = DeploymentState(lab_name="demo", topology_file=str(tmp_path / "demo.yml"))
    st.mgmt_anchors = {
        "master": MgmtAnchorState(
            host="master",
            container="clab-dnlab-demo-mgmt-master-mgmt-anchor",
            topology_file="/tmp/dnlab-demo-mgmt-master.clab.yml",
            state="running",
        ),
    }
    inv = {
        "master": cleanup.HostCleanupInventory(
            name="master",
            host="10.0.0.10",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact(
                    "container",
                    "clab-dnlab-demo-mgmt-master-mgmt-anchor",
                    "master",
                    "demo",
                    state="running",
                ),
            ],
        )
    }

    plans = cleanup.build_cleanup_plan(inv, {"demo": st}, grace_seconds=0)
    commands = [a.command for a in plans["demo"].actions]

    assert plans["demo"].protected is False
    assert commands == ["docker rm -f clab-dnlab-demo-mgmt-master-mgmt-anchor"]


def test_unreachable_state_host_blocks_cleanup(tmp_path):
    st = _state(tmp_path)
    inv = {
        "master": cleanup.HostCleanupInventory(name="master", host="10.0.0.10", reachable=True),
        "worker1": cleanup.HostCleanupInventory(name="worker1", host="10.0.0.11", reachable=False, error="down"),
    }
    plans = cleanup.build_cleanup_plan(inv, {"demo": st}, grace_seconds=0)
    assert plans["demo"].protected is True
    assert "host-unreachable:worker1" in plans["demo"].reasons


def test_recent_artifact_blocks_cleanup():
    inv = {
        "master": cleanup.HostCleanupInventory(
            name="master",
            host="10.0.0.10",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact("container", "dnlab-demo-dns", "master", "demo", state="exited", age_seconds=10),
            ],
        )
    }
    plans = cleanup.build_cleanup_plan(inv, {}, grace_seconds=600)
    assert plans["demo"].protected is True
    assert plans["demo"].actions == []


def test_empty_per_lab_network_is_cleaned_but_shared_and_attached_are_skipped():
    inv = {
        "master": cleanup.HostCleanupInventory(
            name="master",
            host="10.0.0.10",
            reachable=True,
            artifacts=[
                cleanup.CleanupArtifact("network", "mgmt-demo", "master", "demo", metadata={"containers": 0}),
                cleanup.CleanupArtifact("network", "mgmt-demo-busy", "master", "demo", metadata={"containers": 1}),
                cleanup.CleanupArtifact("network", "dnlab-jumphost", "master", "", shared=True, metadata={"containers": 0}),
            ],
        )
    }
    plans = cleanup.build_cleanup_plan(inv, {}, grace_seconds=0)
    commands = [a.command for a in plans["demo"].actions]
    assert "docker network rm mgmt-demo" in commands
    assert all("mgmt-demo-busy" not in cmd for cmd in commands)
    assert all("dnlab-jumphost" not in cmd for cmd in commands)


def test_state_derived_vxlan_and_mgmt_interfaces_are_cleaned(tmp_path):
    st = _state(tmp_path)
    inv = {
        "master": cleanup.HostCleanupInventory(name="master", host="10.0.0.10", reachable=True),
        "worker1": cleanup.HostCleanupInventory(name="worker1", host="10.0.0.11", reachable=True),
    }
    plans = cleanup.build_cleanup_plan(inv, {"demo": st}, grace_seconds=0)
    commands = [a.command for a in plans["demo"].actions]
    assert "ip link delete vx-demo-mgmt" in commands
    assert "ip link delete br-demo" in commands
    assert "ip link delete vrf-demo" in commands
    assert "ip link delete vx-vxabc-R1-e1"[:30] in " ".join(commands)


def test_reconcile_once_dry_run_writes_report_without_mutating(tmp_path):
    _state(tmp_path)
    clients = {
        "master": FakeClient(
            "master",
            containers="dnlab-demo-dns\texited\t2020-01-01 00:00:00 +0000 UTC",
            networks=["mgmt-demo", "dnlab-jumphost"],
            network_counts={"mgmt-demo": 0, "dnlab-jumphost": 0},
            interfaces=["1: vx-demo-mgmt@if1"],
        ),
        "worker1": FakeClient("worker1"),
    }
    state_file = tmp_path / "cleanup.json"
    report = cleanup.reconcile_once(
        _hosts(),
        state_file=state_file,
        topologies_dir=tmp_path,
        clients=clients,
        dry_run=True,
        grace_seconds=0,
    )
    assert report.dry_run is True
    assert state_file.exists()
    assert any(a.command == "docker rm -f dnlab-demo-dns" for a in report.labs["demo"].actions)
    assert all(not any(cmd.startswith("docker rm -f") for cmd in c.commands) for c in clients.values())
    assert json.loads(state_file.read_text())["labs"]["demo"]["protected"] is False


def test_reconcile_once_execute_runs_cleanup_commands(tmp_path):
    _state(tmp_path)
    clients = {
        "master": FakeClient("master", containers="dnlab-demo-dns\texited\t2020-01-01 00:00:00 +0000 UTC"),
        "worker1": FakeClient("worker1"),
    }
    report = cleanup.reconcile_once(
        _hosts(),
        state_file=tmp_path / "cleanup.json",
        topologies_dir=tmp_path,
        clients=clients,
        dry_run=False,
        grace_seconds=0,
    )
    assert any(cmd == "docker rm -f dnlab-demo-dns" for cmd in clients["master"].commands)
    assert any(a.executed and a.ok for a in report.labs["demo"].actions)


def test_cli_cleanup_labs_json(monkeypatch):
    class Report:
        dry_run = True
        labs = {}

        def to_dict(self):
            return {"labs": {}, "dry_run": True}

    monkeypatch.setattr(cli_mod, "Path", Path)
    monkeypatch.setattr(cli_mod, "_setup_logging", lambda debug=False: None)
    monkeypatch.setattr("dnlab_multinode.services.hosts_config.load_hosts_config", lambda _path=None: _hosts())
    monkeypatch.setattr("dnlab_multinode.services.lab_cleanup.reconcile_once", lambda *a, **kw: Report())

    result = CliRunner().invoke(cli_mod.main, ["cleanup-labs", "--json"])

    assert result.exit_code == 0
    assert '"dry_run": true' in result.output
