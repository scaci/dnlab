"""Tests for destroy-controller compatibility cleanup."""

from dnlab_multinode.controllers.destroy import DestroyController
from dnlab_multinode.models.state import DeploymentState


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
