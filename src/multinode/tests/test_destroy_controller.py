"""Tests for destroy-controller compatibility cleanup."""

from dnlab_multinode.controllers.destroy import DestroyController
from dnlab_multinode.models.state import DeploymentState, NodeRuntimeState
from tests.conftest import make_topology


class FakeClient:
    def __init__(self, name: str, fail: bool = False):
        self.name = name
        self.fail = fail
        self.commands: list[str] = []

    def run(self, cmd, *args, **kwargs):
        self.commands.append(cmd)
        if self.fail:
            raise RuntimeError("boom")
        return ""

    def connect(self):
        self.commands.append("connect")

    def close(self):
        self.commands.append("close")

    def destroy_clab(self, topology_file, *, keep_mgmt_net=False):
        suffix = " --keep-mgmt-net" if keep_mgmt_net else ""
        self.commands.append(f"destroy {topology_file}{suffix}")


def test_destroy_legacy_logging_removes_old_artifacts_best_effort():
    master = FakeClient("master")
    worker = FakeClient("worker1")
    ctrl = DestroyController("/tmp/demo.yml")
    ctrl._state = DeploymentState(lab_name="demo", topology_file="/tmp/demo.yml")
    ctrl._clients = {"master": master, "worker1": worker}

    ctrl._destroy_legacy_logging()

    assert any("docker rm -f dnlab-demo-log-shipper" in cmd for cmd in master.commands)
    assert any("docker rm -f dnlab-demo-log-shipper" in cmd for cmd in worker.commands)
    assert any("docker rm -f dnlab-demo-syslog" in cmd for cmd in master.commands)
    assert any("docker volume rm dnlab-demo-logs" in cmd for cmd in master.commands)
    assert ctrl._errors == []


def test_destroy_legacy_logging_ignores_cleanup_errors():
    master = FakeClient("master", fail=True)
    worker = FakeClient("worker1", fail=True)
    ctrl = DestroyController("/tmp/demo.yml")
    ctrl._state = DeploymentState(lab_name="demo", topology_file="/tmp/demo.yml")
    ctrl._clients = {"master": master, "worker1": worker}

    ctrl._destroy_legacy_logging()

    assert ctrl._errors == []


def test_per_host_destroy_is_deduplicated_and_keeps_management_network():
    master = FakeClient("master")
    ctrl = DestroyController("/tmp/demo.yml")
    ctrl._state = DeploymentState(
        lab_name="demo",
        topology_file="/tmp/demo.yml",
        runtime_mode="per-host-apply",
    )
    ctrl._state.node_runtime = {
        name: NodeRuntimeState(
            node=name,
            host="master",
            topology_file="/tmp/dnlab-demo-master.clab.yml",
        )
        for name in ("r1", "r2")
    }
    ctrl._clients = {"master": master}

    ctrl._destroy_clab()

    assert master.commands == [
        "destroy /tmp/dnlab-demo-master.clab.yml --keep-mgmt-net",
    ]


def test_per_host_destroy_run_skips_mgmt_anchor_phase(monkeypatch, tmp_path):
    topo = make_topology(name="demo", num_workers=0)
    state = DeploymentState(
        lab_name="demo",
        topology_file=str(tmp_path / "demo.yml"),
        runtime_mode="per-host-apply",
    )
    phases = []

    monkeypatch.setattr(
        "dnlab_multinode.services.config.parse_topology",
        lambda *args, **kwargs: topo,
    )
    monkeypatch.setattr(
        "dnlab_multinode.controllers.destroy.state_svc.load_state",
        lambda *args, **kwargs: state,
    )
    monkeypatch.setattr(
        "dnlab_multinode.controllers.destroy.state_svc.delete_state",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "dnlab_multinode.services.ssh.create_clients",
        lambda hosts: {"master": FakeClient("master")},
    )

    def fake_phase(self, phase, detail, fn, *args, **kwargs):
        phases.append(phase)
        if phase == "destroy-mgmt-anchor":
            raise AssertionError("per-host-apply destroy must not run mgmt-anchor phase")
        return None

    monkeypatch.setattr(DestroyController, "_phase", fake_phase)

    DestroyController(str(tmp_path / "demo.yml")).run()

    assert "destroy-dnlab" in phases
    assert "destroy-mgmt-anchor" not in phases
