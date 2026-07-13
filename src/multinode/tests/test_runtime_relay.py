"""Tests for the per-host runtime relay service."""

import os
import time
from unittest.mock import MagicMock

import pytest

from dnlab_multinode.models.schedule import HostAssignment, SchedulePlan
from dnlab_multinode.services import runtime_relay as relay_svc
from dnlab_multinode.utils.ids import runtime_relay_port
from dnlab_multinode.utils.naming import (
    micro_vd_container_name,
    runtime_relay_container_name,
)
from tests.conftest import make_topology
from tests.runtime_relay_import import load_runtime_relay_module


relay_daemon = load_runtime_relay_module()


class FakeRelaySocket:
    def __init__(self):
        self.read_fd, self.write_fd = os.pipe()
        self.sent: list[bytes] = []

    def fileno(self):
        return self.read_fd

    def sendall(self, data: bytes):
        self.sent.append(data)

    def recv(self, _size: int, _flags: int = 0):
        return b""

    def disconnect(self):
        os.write(self.write_fd, b"x")

    def close(self):
        for fd in (self.read_fd, self.write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def _mock_host_client(image_exists: bool = True, runs_ok: bool = True) -> MagicMock:
    client = MagicMock()

    def run_no_check(cmd, *_, **__):
        if "docker image inspect" in cmd:
            return (0 if image_exists else 1), "", ""
        if "docker inspect" in cmd and "State.Running" in cmd:
            return (0 if runs_ok else 1), ("true" if runs_ok else "false"), ""
        if "docker logs" in cmd:
            return 0, "(fake logs)", ""
        return 0, "", ""

    client.run_no_check.side_effect = run_no_check
    client.run.return_value = ""
    return client


def _plan_with_assignments(lab_name: str, mapping: dict[str, list[str]]) -> SchedulePlan:
    assignments = {
        host: HostAssignment(host_name=host, host_ip="", vd_names=vds)
        for host, vds in mapping.items()
    }
    return SchedulePlan(lab_name=lab_name, assignments=assignments)


def test_deploy_runtime_relays_one_per_host_with_vds():
    topo = make_topology(name="demo", num_workers=2)
    plan = _plan_with_assignments("demo", {
        "master": ["R1"],
        "worker1": ["R2"],
        "worker2": [],
    })
    clients = {
        "master": _mock_host_client(),
        "worker1": _mock_host_client(),
        "worker2": _mock_host_client(),
    }

    results = relay_svc.deploy_runtime_relays(
        topo,
        plan,
        clients,
        {"master": "10.0.0.10", "worker1": "10.0.0.11", "worker2": "10.0.0.12"},
        api_key="secret",
    )

    assert set(results) == {"master", "worker1"}
    assert results["master"]["container"] == runtime_relay_container_name("demo")
    assert results["master"]["bind_ip"] == "10.0.0.10"
    assert results["master"]["port"] == runtime_relay_port("demo")
    assert results["master"]["api_key"] == "secret"
    assert results["master"]["allowed"] == [micro_vd_container_name("demo", "R1")]


def test_deploy_runtime_relay_command_is_lab_scoped_and_host_local():
    topo = make_topology(name="demo")
    plan = _plan_with_assignments("demo", {"master": ["R1", "R2"]})
    client = _mock_host_client()

    relay_svc.deploy_runtime_relays(
        topo,
        plan,
        {"master": client},
        {"master": "10.0.0.10"},
        api_key="secret with space",
    )

    run_cmds = [c.args[0] for c in client.run.call_args_list]
    run_cmd = next(cmd for cmd in run_cmds if "docker run -d" in cmd)
    expected_list = (
        f"{micro_vd_container_name('demo', 'R1')} "
        f"{micro_vd_container_name('demo', 'R2')}"
    )

    assert "--network host" in run_cmd
    assert "/var/run/docker.sock:/var/run/docker.sock" in run_cmd
    assert "-e RELAY_BIND=10.0.0.10" in run_cmd
    assert f"-e RELAY_PORT={runtime_relay_port('demo')}" in run_cmd
    assert "-e RELAY_API_KEY='secret with space'" in run_cmd
    assert f"-e RELAY_ALLOWED_CONTAINERS='{expected_list}'" in run_cmd


def test_deploy_runtime_relay_uses_runtime_container_names_when_provided():
    topo = make_topology(name="demo")
    plan = _plan_with_assignments("demo", {"master": ["R1", "R2"]})
    client = _mock_host_client()

    relay_svc.deploy_runtime_relays(
        topo,
        plan,
        {"master": client},
        {"master": "10.0.0.10"},
        api_key="secret",
        runtime_containers={
            "R1": "clab-demo-R1",
            "R2": "clab-demo-R2",
        },
    )

    run_cmds = [c.args[0] for c in client.run.call_args_list]
    run_cmd = next(cmd for cmd in run_cmds if "docker run -d" in cmd)

    assert "-e RELAY_ALLOWED_CONTAINERS='clab-demo-R1 clab-demo-R2'" in run_cmd
    assert micro_vd_container_name("demo", "R1") not in run_cmd


def test_deploy_runtime_relay_missing_image_raises():
    topo = make_topology(name="demo")
    plan = _plan_with_assignments("demo", {"master": ["R1"]})

    with pytest.raises(RuntimeError, match="not found"):
        relay_svc.deploy_runtime_relays(
            topo,
            plan,
            {"master": _mock_host_client(image_exists=False)},
            {"master": "10.0.0.10"},
            api_key="secret",
        )


def test_destroy_runtime_relays_removes_sidecar_on_each_host():
    clients = {"master": MagicMock(), "worker1": MagicMock()}

    relay_svc.destroy_runtime_relays("demo", clients)

    for client in clients.values():
        run_cmds = [c.args[0] for c in client.run.call_args_list]
        assert any(
            f"docker rm -f {runtime_relay_container_name('demo')}" in cmd
            for cmd in run_cmds
        )


def test_connect_cmd_returns_serial_port(monkeypatch):
    monkeypatch.setattr(relay_daemon, "_discover_console_port", lambda _container: "5003")

    cmd, port = relay_daemon._connect_cmd("container1")

    assert port == "5003"
    assert cmd == [
        "docker", "exec", "-it", "container1", "telnet", "127.0.0.1", "5003",
    ]


def test_connect_cmd_shell_fallback_has_no_serial_port(monkeypatch):
    monkeypatch.setattr(relay_daemon, "_discover_console_port", lambda _container: "")

    cmd, port = relay_daemon._connect_cmd("container1")

    assert port is None
    assert cmd[:4] == ["docker", "exec", "-it", "container1"]
    assert "vtysh" in cmd[-1]


def test_relay_pty_disconnect_cleans_container_telnet(monkeypatch):
    relay_sock = FakeRelaySocket()
    cleanup_calls = []

    class FakeProc:
        pid = os.getpid()

        def __init__(self, args, **_kwargs):
            self.args = args
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return self.returncode

    def fake_run(args, **_kwargs):
        cleanup_calls.append(args)
        return MagicMock(returncode=0)

    monkeypatch.setattr(relay_daemon.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(relay_daemon.subprocess, "run", fake_run)

    try:
        relay_sock.disconnect()
        relay_daemon._relay_pty(
            relay_sock,
            ["docker", "exec", "-it", "container1", "telnet", "127.0.0.1", "5000"],
            "container1",
            "5000",
        )
    finally:
        relay_sock.close()

    assert cleanup_calls == [[
        "docker",
        "exec",
        "container1",
        "sh",
        "-c",
        "pkill -f '[t]elnet 127[.]0[.]0[.]1 5000' || true",
    ]]


def test_relay_pty_shell_fallback_does_not_cleanup_telnet(monkeypatch):
    relay_sock = FakeRelaySocket()
    cleanup_calls = []

    class FakeProc:
        pid = os.getpid()

        def __init__(self, args, **_kwargs):
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return self.returncode

    monkeypatch.setattr(relay_daemon.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(
        relay_daemon.subprocess,
        "run",
        lambda args, **_kwargs: cleanup_calls.append(args),
    )

    try:
        relay_sock.disconnect()
        relay_daemon._relay_pty(
            relay_sock,
            ["docker", "exec", "-it", "container1", "sh"],
            "container1",
            None,
        )
    finally:
        relay_sock.close()

    assert cleanup_calls == []


def test_container_telnet_cleanup_errors_are_best_effort(monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise TimeoutError("docker stuck")

    monkeypatch.setattr(relay_daemon.subprocess, "run", fake_run)

    relay_daemon._cleanup_container_telnet("container1", "5000")


def test_stream_logs_flushes_small_chunks_immediately(monkeypatch):
    read_fd, write_fd = os.pipe()
    relay_sock = FakeRelaySocket()

    class FakeStdout:
        def fileno(self):
            return read_fd

    class FakeProc:
        stdout = FakeStdout()

        def __init__(self, *_, **__):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(relay_daemon.subprocess, "Popen", FakeProc)
    try:
        import threading

        thread = threading.Thread(
            target=relay_daemon._stream_logs,
            args=(relay_sock, "container1", "200", "1"),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not relay_sock.sent:
            time.sleep(0.01)
        assert relay_sock.sent == [b"OK\n"]

        os.write(write_fd, b"x\n")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and b"x\n" not in relay_sock.sent:
            time.sleep(0.01)
        assert b"x\n" in relay_sock.sent

        relay_sock.disconnect()
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        relay_sock.close()
        for fd in (read_fd, write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_stream_logs_builds_history_and_follow_commands(monkeypatch):
    calls = []

    class FakeStdout:
        def __init__(self):
            self.read_fd, self.write_fd = os.pipe()
            os.close(self.write_fd)

        def fileno(self):
            return self.read_fd

    class FakeProc:
        def __init__(self, args, **_kwargs):
            calls.append(args)
            self.stdout = FakeStdout()
            self.returncode = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(relay_daemon.subprocess, "Popen", FakeProc)

    for tail, follow in (("all", "0"), ("200", "1")):
        relay_sock = FakeRelaySocket()
        try:
            relay_daemon._stream_logs(relay_sock, "container1", tail, follow)
            assert relay_sock.sent == [b"OK\n"]
        finally:
            relay_sock.close()

    assert calls == [
        ["docker", "logs", "container1"],
        ["docker", "logs", "-f", "--tail=200", "container1"],
    ]
