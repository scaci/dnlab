import io
from pathlib import Path

import pytest

from dnlab_multinode.models.schedule import HostAssignment, SchedulePlan
from dnlab_multinode.models.topology import (
    DistributedTopology, InfraHost, JumphostConfig, JumphostNet, MgmtConfig,
    VDNode,
)
from dnlab_multinode.services.persistence import (
    PersistenceError, _adopt_legacy_overlay_paths, _migrate_overlay, load_placement_history,
    load_placement_preferences,
    placement_file_path, save_placement_history,
)


def _topo() -> DistributedTopology:
    master = InfraHost("master", "10.0.0.1", "root", "/root/.ssh/id")
    return DistributedTopology(
        name="lab1",
        master=master,
        workers={},
        underlay_iface="eth0",
        jumphost=JumphostConfig(),
        jumphost_net=JumphostNet("dnlab-jh", "br-jh", "192.168.100.0/24", "192.168.100.1"),
        nodes={
            "r1": VDNode("r1", "vr-x", "vrnetlab/foo:1.0-dnlab"),
            "r2": VDNode("r2", "linux", "alpine:latest"),
        },
        links=[],
        mgmt=MgmtConfig("mgmt", "br-mgmt", "172.20.0.0/24", "172.20.0.1"),
    )


def test_save_placement_history_only_persistent_vds(tmp_path: Path):
    topo = _topo()
    topo.nodes["r1"].persist_id = "stable-r1"
    plan = SchedulePlan(
        lab_name="lab1",
        assignments={
            "master": HostAssignment(
                host_name="master",
                host_ip="10.0.0.1",
                vd_names=["r1", "r2"],
            ),
        },
    )

    path = save_placement_history(topo, plan, tmp_path)

    assert path == placement_file_path("lab1", tmp_path)
    assert load_placement_history("lab1", tmp_path) == {"stable-r1": "master"}
    assert load_placement_preferences(topo, tmp_path) == {"r1": "master"}


def test_load_placement_preferences_accepts_legacy_node_keys(tmp_path: Path):
    topo = _topo()
    topo.nodes["r1"].persist_id = "stable-r1"
    placement_file_path("lab1", tmp_path).write_text('{"placements": {"r1": "master"}}')

    assert load_placement_preferences(topo, tmp_path) == {"r1": "master"}


def test_load_placement_history_ignores_missing_file(tmp_path: Path):
    assert load_placement_history("missing", tmp_path) == {}


class _FakeClient:
    def __init__(self, name: str, cleanup_rc: int = 0):
        self.name = name
        self.host = name
        self.user = "root"
        self.key_path = "/root/.ssh/id"
        self.cleanup_rc = cleanup_rc
        self.run_no_check_calls: list[tuple[str, int]] = []

    def run(self, command: str, timeout: int = 30, check: bool = True) -> str:
        if command.startswith("du -sb "):
            return "1234"
        return ""

    def run_no_check(self, command: str, timeout: int = 30):
        self.run_no_check_calls.append((command, timeout))
        if command.startswith("rm -rf -- "):
            return self.cleanup_rc, "", "cleanup failed"
        return 0, "", ""


class _FakePopen:
    source_rc = 0
    destination_rc = 0

    def __init__(self, _cmd, stdout=None, stderr=None, stdin=None):
        self.is_source = stdin is None
        self.returncode = (
            self.source_rc if self.is_source else self.destination_rc
        )
        self.stdout = io.BytesIO(b"overlay-data") if stdout is not None else None
        self.stderr = io.BytesIO(b"")

    def communicate(self, timeout=None):
        return b"", self.stderr.read()

    def wait(self, timeout=None):
        return self.returncode


def test_migrate_overlay_removes_source_after_success(monkeypatch):
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    _FakePopen.source_rc = 0
    _FakePopen.destination_rc = 0
    src = _FakeClient("worker1")
    dst = _FakeClient("worker2")

    assert _migrate_overlay(_topo(), "r1", src, dst) == 1234

    cleanup_cmds = [
        cmd for cmd, _timeout in src.run_no_check_calls
        if cmd.startswith("rm -rf -- ")
    ]
    assert len(cleanup_cmds) == 1
    assert cleanup_cmds[0].endswith("/lab1/r1")


def test_migrate_overlay_uses_stable_persist_id(monkeypatch):
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    _FakePopen.source_rc = 0
    _FakePopen.destination_rc = 0
    topo = _topo()
    topo.nodes["r1"].persist_id = "stable-r1"
    src = _FakeClient("worker1")
    dst = _FakeClient("worker2")

    assert _migrate_overlay(topo, "r1", src, dst) == 1234

    cleanup_cmds = [
        cmd for cmd, _timeout in src.run_no_check_calls
        if cmd.startswith("rm -rf -- ")
    ]
    assert cleanup_cmds[0].endswith("/lab1/stable-r1")


def test_adopt_legacy_overlay_moves_name_path_to_stable_id():
    topo = _topo()
    topo.nodes["r1"].persist_id = "stable-r1"
    client = _FakeClient("worker1")

    _adopt_legacy_overlay_paths(topo, "r1", {"worker1": client})

    command = client.run_no_check_calls[0][0]
    assert "/lab1/r1" in command
    assert "/lab1/stable-r1" in command
    assert "mv --" in command


def test_migrate_overlay_keeps_source_when_destination_copy_fails(monkeypatch):
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    _FakePopen.source_rc = 0
    _FakePopen.destination_rc = 1
    src = _FakeClient("worker1")
    dst = _FakeClient("worker2")

    with pytest.raises(PersistenceError, match="destination tar failed"):
        _migrate_overlay(_topo(), "r1", src, dst)

    assert not [
        cmd for cmd, _timeout in src.run_no_check_calls
        if cmd.startswith("rm -rf -- ")
    ]


def test_migrate_overlay_reports_cleanup_failure(monkeypatch):
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    _FakePopen.source_rc = 0
    _FakePopen.destination_rc = 0
    src = _FakeClient("worker1", cleanup_rc=1)
    dst = _FakeClient("worker2")

    with pytest.raises(PersistenceError, match="failed to remove source"):
        _migrate_overlay(_topo(), "r1", src, dst)
