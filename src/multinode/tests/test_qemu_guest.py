from __future__ import annotations

import pytest

from dnlab_multinode.services import qemu_guest


class FakeClient:
    name = "worker1"

    def __init__(self, responses=None, rc=0, err=""):
        self.responses = list(responses) if responses is not None else [(rc, "", err)]
        self.calls = []

    def run_no_check(self, command, timeout=30):
        self.calls.append((command, timeout))
        if self.responses:
            return self.responses.pop(0)
        return 0, "", ""


def test_image_uses_persistent_disk_contract():
    assert qemu_guest.image_uses_persistent_disk("vrnetlab/dnlab_frr:latest")
    assert qemu_guest.image_uses_persistent_disk("vrnetlab/cisco_n9kv:10.5-dnlab")
    assert not qemu_guest.image_uses_persistent_disk("vrnetlab/cisco_n9kv:10.5")


def test_graceful_powerdown_uses_qemu_monitor_and_waits():
    client = FakeClient()

    qemu_guest.graceful_powerdown_container(client, "clab-lab-r1", timeout=12)

    assert len(client.calls) == 1
    command, timeout = client.calls[0]
    assert timeout == 27
    assert "system_powerdown" in command
    assert "/.venv/bin/python3" in command
    assert "pgrep -f" in command
    assert "qemu-system" in command
    assert "docker inspect" in command
    assert "clab-lab-r1" in command


def test_graceful_powerdown_timeout_falls_back_to_docker_stop():
    client = FakeClient(responses=[(124, "", ""), (0, "", "")])

    qemu_guest.graceful_powerdown_container(client, "clab-lab-r1", timeout=5)

    assert len(client.calls) == 2
    assert "docker stop --time 5 clab-lab-r1" in client.calls[1][0]


def test_graceful_powerdown_fails_when_docker_stop_fallback_fails():
    client = FakeClient(responses=[(124, "", ""), (1, "", "still running")])

    with pytest.raises(qemu_guest.GuestShutdownError):
        qemu_guest.graceful_powerdown_container(client, "clab-lab-r1", timeout=5)


def test_graceful_powerdown_falls_back_to_docker_stop_when_monitor_unavailable():
    client = FakeClient(responses=[(3, "", "monitor unavailable"), (0, "", "")])

    qemu_guest.graceful_powerdown_container(client, "clab-lab-r1", timeout=5)

    assert len(client.calls) == 2
    assert "system_powerdown" in client.calls[0][0]
    assert "docker stop --time 5 clab-lab-r1" in client.calls[1][0]


def test_graceful_powerdown_falls_back_to_docker_stop_when_python_missing():
    client = FakeClient(responses=[(127, "", "python unavailable"), (0, "", "")])

    qemu_guest.graceful_powerdown_container(client, "clab-lab-r1", timeout=5)

    assert len(client.calls) == 2
    assert "docker stop --time 5 clab-lab-r1" in client.calls[1][0]
