from click.testing import CliRunner

from dnlab_multinode import cli


class _FakeNode:
    name = "R1"
    host = "worker1"
    kind = "linux"
    apply_mode = "live"
    mgmt_ipv4 = "172.20.0.11"
    state = "running"


class _FakeHost:
    name = "worker1"
    host = "10.0.0.11"
    reachable = True
    error = ""
    vd_count = 1
    cpu_used = 1
    ram_mb_used = 256


class _FakeInfra:
    dns = {}
    jumphost = {}
    runtime_relays = {}


class _FakeReport:
    deployed = True
    lab_name = "lab"
    deployed_at = "2026-07-10T15:00:00"
    runtime_mode = "per-host-apply"
    reconcile_required = False
    host_apply_status = {"worker1": "applied"}
    containerlab_versions = {"worker1": "0.77.0"}
    host_apply_plan = {
        "worker1": [
            {
                "action": "added links",
                "details": "R1:eth1 -- R2:eth1",
                "nodes": ["R1", "R2"],
            }
        ]
    }
    hosts = {"worker1": _FakeHost()}
    nodes = {"R1": _FakeNode()}
    cross_host_links = 0
    infra = _FakeInfra()

    def to_dict(self):
        return {}


def test_get_status_table_shows_node_apply_mode(monkeypatch):
    monkeypatch.setattr(cli, "_setup_logging", lambda debug=False: None)

    class FakeStatusController:
        def __init__(self, topo, hosts_file=None):
            self.topo = topo
            self.hosts_file = hosts_file

        def run(self):
            return _FakeReport()

    monkeypatch.setattr(
        "dnlab_multinode.controllers.status.StatusController",
        FakeStatusController,
    )

    result = CliRunner().invoke(
        cli.main,
        ["get-status", "-t", "/tmp/lab.yml"],
    )

    assert result.exit_code == 0
    assert "Runtime mode:" in result.output
    assert "per-host-apply" in result.output
    assert "Virtual Devices" in result.output
    assert "Apply" in result.output
    assert "added links" in result.output
    assert "live" in result.output
