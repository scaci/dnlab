"""Shared SSH client for the per-lab jumphost container.

Every deployed lab ships with a jumphost container named
``dnlab-<lab>-jumphost`` that sits on the lab's management network and
has routing / DNS to every VD. The GUI reaches it via SSH as
``labuser`` with the key configured in ``settings.GUI_SSH_KEY``.

The dockerized GUI shares no docker network with the per-lab jumphost,
so it connects to the SSH port the jumphost publishes on the master host
(``settings.JUMPHOST_HOST`` + the ``ssh_port`` from the multinode status
report). Pass ``ssh_port`` to reach it that way; omit it to fall back to
resolving the jumphost by container name (legacy GUI-on-master).

Entry point:

* :meth:`JumphostClient.exec_stream` — spawn a remote command and
  stream its stdout line-by-line. Used by the ``vd log`` streamer.

The client is a one-shot context manager — open it, do the work, let
``__exit__`` tear the SSH connection down. Paramiko connections are
cheap enough that caching would be premature optimisation.
"""

from __future__ import annotations

import logging
import socket

import paramiko

from app.config import settings

log = logging.getLogger(__name__)


class JumphostError(Exception):
    """Raised when the jumphost is unreachable or the command fails."""


class JumphostClient:
    """Paramiko SSH client scoped to a single lab's jumphost container.

    Usage::

        with JumphostClient("demo") as jh:
            for line in jh.exec_stream("vd log -f clab-demo-NX1"):
                ws.send(line)
    """

    def __init__(
        self,
        lab_name: str,
        *,
        user: str | None = None,
        key_path: str | None = None,
        connect_timeout: float = 10.0,
        ssh_host: str | None = None,
        ssh_port: int | None = None,
    ) -> None:
        self.lab_name = lab_name
        self.container = f"dnlab-{lab_name}-jumphost"
        self.user = user or settings.JUMPHOST_USER
        self.key_path = key_path or settings.GUI_SSH_KEY
        self.connect_timeout = connect_timeout
        # Reach the jumphost via its published SSH port on the master
        # (dockerized GUI), or by container name on a shared docker
        # network (legacy GUI-on-master) when no port is given.
        if ssh_port:
            self.hostname = ssh_host or settings.JUMPHOST_HOST
            self.port = ssh_port
        else:
            self.hostname = self.container
            self.port = 22
        self._ssh: paramiko.SSHClient | None = None

    # ── context ───────────────────────────────────────────────────────
    def __enter__(self) -> "JumphostClient":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        ssh = paramiko.SSHClient()
        # Jumphost container host keys change on every redeploy, so
        # AutoAddPolicy + user_known_hosts works better than strict
        # checking here. Production hardening (M6) may revisit this.
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                hostname=self.hostname,
                port=self.port,
                username=self.user,
                key_filename=self.key_path,
                timeout=self.connect_timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except (paramiko.SSHException, socket.error) as exc:
            raise JumphostError(
                f"cannot reach jumphost '{self.container}' "
                f"({self.hostname}:{self.port}) as {self.user}: {exc}"
            ) from exc
        self._ssh = ssh
        log.debug(
            "jumphost %s: ssh connected via %s:%d",
            self.container, self.hostname, self.port,
        )

    def close(self) -> None:
        if self._ssh is not None:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None

    # ── operations ────────────────────────────────────────────────────
    def exec_stream(self, command: str, *, line_buffered: bool = True):
        """Run ``command`` and yield stdout lines (text) as they arrive.

        stderr is captured into the log for debugging. The remote
        process is terminated when the generator is closed.
        """
        if self._ssh is None:
            raise JumphostError("jumphost client is not open")
        transport = self._ssh.get_transport()
        if transport is None:
            raise JumphostError("jumphost transport closed")
        chan = transport.open_session()
        try:
            chan.exec_command(command)
            stdout = chan.makefile("r", -1 if line_buffered else 4096)
            for line in stdout:
                yield line.rstrip("\n")
        finally:
            try:
                chan.close()
            except Exception:
                pass
