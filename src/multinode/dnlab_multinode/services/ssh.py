"""SSH/SFTP operations via paramiko."""

from __future__ import annotations

import logging
import os
import shlex
import threading
from pathlib import Path
from typing import Callable

import paramiko

log = logging.getLogger(__name__)

# Timeouts
_CONNECT_TIMEOUT = 10
_CMD_TIMEOUT = 30
_DEPLOY_TIMEOUT = 300


class SSHError(Exception):
    """Raised when an SSH command fails."""
    def __init__(self, host: str, command: str, stderr: str, returncode: int = -1):
        self.host = host
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"[{host}] Command failed (rc={returncode}): {command}\n  stderr: {stderr}")


class SSHClient:
    """Wrapper around paramiko for executing remote commands and SFTP."""

    def __init__(self, host: str, user: str, key_path: str, name: str = ""):
        self.host = host
        self.user = user
        self.key_path = os.path.expanduser(key_path)
        self.name = name or host
        self._client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        log.debug("[%s] Connecting SSH %s@%s (key=%s)", self.name, self.user, self.host, self.key_path)
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            hostname=self.host,
            username=self.user,
            key_filename=self.key_path,
            timeout=_CONNECT_TIMEOUT,
            look_for_keys=False,
            allow_agent=False,
        )
        log.info("[%s] SSH connected to %s", self.name, self.host)

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            log.debug("[%s] SSH disconnected", self.name)

    def run(self, command: str, timeout: int = _CMD_TIMEOUT, check: bool = True) -> str:
        """Execute a command and return stdout. Raises SSHError on non-zero exit if check=True."""
        if not self._client:
            raise RuntimeError(f"[{self.name}] SSH not connected")

        log.debug("[%s] exec: %s", self.name, command)
        _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()

        if rc != 0:
            log.warning("[%s] Command rc=%d: %s\n  stderr: %s", self.name, rc, command, err)
            if check:
                raise SSHError(self.host, command, err, rc)
        elif err:
            # Containerlab can report endpoint deployment failures as warnings
            # on stderr while still returning rc=0.  Keep that diagnostic in
            # the dNLab log instead of silently discarding it.
            log.warning(
                "[%s] Command succeeded with stderr: %s\n  stderr: %s",
                self.name, command, err[:4000],
            )

        log.debug("[%s] result rc=%d out_len=%d", self.name, rc, len(out))
        return out

    def run_no_check(self, command: str, timeout: int = _CMD_TIMEOUT) -> tuple[int, str, str]:
        """Execute a command and return (rc, stdout, stderr) without raising."""
        if not self._client:
            raise RuntimeError(f"[{self.name}] SSH not connected")

        log.debug("[%s] exec (no_check): %s", self.name, command)
        _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        return rc, out, err

    def upload_text(self, content: str, remote_path: str) -> None:
        """Write text content to a remote file via SFTP."""
        if not self._client:
            raise RuntimeError(f"[{self.name}] SSH not connected")

        log.debug("[%s] SFTP upload → %s (%d bytes)", self.name, remote_path, len(content))
        sftp = self._client.open_sftp()
        try:
            with sftp.file(remote_path, "w") as f:
                f.write(content)
        finally:
            sftp.close()

    def deploy_clab(self, topology_file: str, *, reconfigure: bool = False) -> str:
        """Run containerlab deploy and return output."""
        cmd = f"containerlab deploy -t {shlex.quote(topology_file)}"
        if reconfigure:
            cmd += " --reconfigure"
        return self.run(
            cmd,
            timeout=_DEPLOY_TIMEOUT,
        )

    def validate_clab(self, topology_file: str) -> str:
        """Validate a topology with the same checks used by deploy."""
        return self.run(
            f"containerlab validate -t {shlex.quote(topology_file)}",
            timeout=_DEPLOY_TIMEOUT,
        )

    def apply_clab(self, topology_file: str, *, dry_run: bool = False) -> str:
        """Deploy or reconcile a topology."""
        cmd = f"containerlab apply -t {shlex.quote(topology_file)}"
        if dry_run:
            cmd += " --dry-run"
        return self.run(cmd, timeout=_DEPLOY_TIMEOUT)

    def lifecycle_clab(self, action: str, topology_file: str, node: str) -> str:
        if action not in {"start", "stop", "restart"}:
            raise ValueError(f"unsupported containerlab lifecycle action: {action}")
        return self.run(
            f"containerlab {action} -t {shlex.quote(topology_file)} --node {shlex.quote(node)}",
            timeout=_DEPLOY_TIMEOUT,
        )

    def inspect_clab_interfaces(self, topology_file: str) -> str:
        """Return Containerlab interface inspection as JSON text."""
        return self.run(
            f"containerlab inspect interfaces -t {shlex.quote(topology_file)} --format json",
            timeout=_DEPLOY_TIMEOUT,
        )

    def inspect_clab(self, topology_file: str) -> str:
        """Return Containerlab node inspection as JSON text."""
        return self.run(
            f"containerlab inspect -t {shlex.quote(topology_file)} --format json",
            timeout=_DEPLOY_TIMEOUT,
        )

    def stream_clab_events(
        self,
        topology_file: str,
        *,
        stop_event: threading.Event,
        on_line: Callable[[str], None],
        window_seconds: int = 65,
    ) -> tuple[int, str]:
        """Stream Containerlab events for a bounded window.

        ``containerlab events`` is intentionally long-running. The bounded
        ``timeout`` wrapper gives callers a natural reconnect point and avoids
        leaving remote SSH commands behind forever if a watcher is stopped.
        """
        if not self._client:
            raise RuntimeError(f"[{self.name}] SSH not connected")

        seconds = max(5, int(window_seconds))
        cmd = (
            f"timeout {seconds}s containerlab events "
            f"-t {shlex.quote(topology_file)} --format json --initial-state"
        )
        log.debug("[%s] exec stream: %s", self.name, cmd)
        _, stdout, stderr = self._client.exec_command(cmd, timeout=seconds + 15)
        channel = stdout.channel
        try:
            for line in stdout:
                if stop_event.is_set():
                    channel.close()
                    break
                line = line.strip()
                if line:
                    on_line(line)
        finally:
            rc = channel.recv_exit_status()
        err = stderr.read().decode(errors="replace").strip()
        return rc, err

    def destroy_clab(self, topology_file: str, *, keep_mgmt_net: bool = False) -> str:
        """Run containerlab destroy and return output."""
        cmd = f"containerlab destroy -t {shlex.quote(topology_file)} --cleanup"
        if keep_mgmt_net:
            cmd += " --keep-mgmt-net"
        return self.run(
            cmd,
            timeout=_DEPLOY_TIMEOUT,
            check=False,
        )

    def __enter__(self) -> SSHClient:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def create_clients(hosts: dict) -> dict[str, SSHClient]:
    """Create SSHClient instances from host defs (InfraHost objects)."""
    from dnlab_multinode.models.topology import InfraHost
    clients = {}
    for name, host in hosts.items():
        clients[name] = SSHClient(
            host=host.host,
            user=host.ssh_user,
            key_path=host.ssh_key,
            name=name,
        )
    return clients
