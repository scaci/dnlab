"""WebSocket-based console service.

The GUI console reaches the VD through the lab-scoped runtime relay. The relay
runs on the Docker host that owns the VD container, authorizes only the lab
allowlist, then opens the container-side serial socket with ``telnet
127.0.0.1:5000..5007``.

The older direct ``docker exec`` path is kept as implementation fallback code
for compatibility, but controllers use the relay path.
"""

import asyncio
import logging
import os
import pty
import signal
import struct
import fcntl
import termios

from fastapi import WebSocket

from app.config import settings
from app.models.lab import ContainerInfo

log = logging.getLogger(__name__)

_READ_SIZE = 4096

# Retry loop for ``ss`` discovery inside the container: kinds such as cisco_iol
# open their loopback port a few seconds after boot. Try for ~10s before
# falling back to the shell.
_DISCOVER_RETRIES = 10
_DISCOVER_INTERVAL_S = 1.0


class ConsoleService:
    """Manages interactive PTY sessions over WebSockets."""

    async def attach_relay(
        self,
        websocket: WebSocket,
        relay: dict,
    ) -> None:
        """Open the console through the lab-scoped runtime relay."""
        from app.services.runtime_relay_client import RuntimeRelayClient

        log.info(
            "Console relay attach: %s → %s:%s",
            relay.get("container"), relay.get("host"), relay.get("port"),
        )
        await RuntimeRelayClient().connect_console(websocket, relay)

    async def attach(
        self,
        websocket: WebSocket,
        container: ContainerInfo,
        lab_name: str | None = None,
        worker_host: dict | None = None,
    ) -> None:
        """Open the container-side serial console and bridge it to the WebSocket."""
        cmd = await self._build_fallback_cmd(container, worker_host)
        log.info("Console attach: %s → %s", container.name, " ".join(cmd))
        cleanup = self._cleanup_spec(container, worker_host, cmd)
        await self._run_session(websocket, cmd, cleanup=cleanup)

    # ── Dispatch builders ────────────────────────────────────────────

    async def _build_fallback_cmd(
        self,
        container: ContainerInfo,
        worker_host: dict | None,
    ) -> list[str]:
        """Build the fallback command:

        * retry-based discovery of a TCP port in LISTEN on 127.0.0.1 inside
          the container (via ``ss``);
        * FRR containers → ``docker exec -it <c> vtysh``;
        * if found → ``docker exec -it <c> telnet 127.0.0.1 <port>``;
        * otherwise → ``docker exec -it <c> sh``.

        Run it on the master (no ``worker_host``) or via SSH to the worker
        hosting the container.
        """
        inner = await self._fallback_inner_cmd(container, worker_host)
        if worker_host:
            return [
                "ssh", "-tt",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ServerAliveInterval=30",
                "-i", settings.GUI_SSH_KEY,
                f"{worker_host['ssh_user']}@{worker_host['host']}",
                f"docker exec -it {container.name} {inner}",
            ]
        return ["docker", "exec", "-it", container.name, *inner.split()]

    async def _fallback_inner_cmd(
        self,
        container: ContainerInfo,
        worker_host: dict | None,
    ) -> str:
        """Return the command to execute inside a non-serial container."""
        if (container.kind or "").lower() == "frr":
            return "vtysh"

        port = await self._discover_loopback_port(container, worker_host)
        if port:
            return f"telnet 127.0.0.1 {port}"
        return "sh"

    async def _discover_loopback_port(
        self,
        container: ContainerInfo,
        worker_host: dict | None,
    ) -> int | None:
        """Return a TCP port in LISTEN on 127.0.0.1/::1 inside the container,
        or ``None`` if none is found after ~10 retries of 1s. The retry helps
        kinds such as cisco_iol that open their loopback port a few seconds
        after boot.
        """
        # ``ss -Htln`` → no-header, tcp, listening, no-resolve. Extract local
        # serial console ports reachable from inside the container. Some
        # vrnetlab launchers bind QEMU consoles to wildcard addresses
        # (``*:5000``) rather than loopback; these are still safe here because
        # the command runs inside the container namespace. Restrict to the
        # agreed 5000..5007 console range.
        ss_cmd = (
            "ss -Htln 2>/dev/null | "
            "awk '$1==\"LISTEN\" && $4 !~ /^127\\.0\\.0\\.11:/ "
            "&& ($4 ~ /^127\\.0\\.0\\.1:/ || $4 ~ /^\\[::1\\]:/ "
            "|| $4 ~ /^\\*:/ || $4 ~ /^0\\.0\\.0\\.0:/ || $4 ~ /^\\[::\\]:/) "
            "{ n=split($4,a,\":\"); if (a[n] ~ /^500[0-7]$/) { print a[n]; exit } }'"
        )
        for attempt in range(_DISCOVER_RETRIES):
            try:
                port = await self._run_in_container(container, worker_host, ss_cmd)
            except Exception as exc:
                log.debug(
                    "discover loopback (%s attempt %d): %s",
                    container.name, attempt, exc,
                )
                port = None
            if port and port.isdigit():
                p = int(port)
                if 1 <= p <= 65535:
                    log.info(
                        "Console %s: fallback port %d discovered on attempt %d",
                        container.name, p, attempt + 1,
                    )
                    return p
            if attempt < _DISCOVER_RETRIES - 1:
                await asyncio.sleep(_DISCOVER_INTERVAL_S)
        log.info(
            "Console %s: no loopback port after %d attempts — "
            "falling back to shell",
            container.name, _DISCOVER_RETRIES,
        )
        return None

    @staticmethod
    async def _run_in_container(
        container: ContainerInfo,
        worker_host: dict | None,
        shell_expr: str,
    ) -> str:
        """Run ``sh -c <shell_expr>`` inside the container and return stripped
        stdout. Used only for discovery: no PTY, non-interactive, short
        timeout.
        """
        docker_cmd = f"docker exec {container.name} sh -c {_shell_quote(shell_expr)}"
        if worker_host:
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ServerAliveInterval=30",
                "-o", "BatchMode=yes",
                "-i", settings.GUI_SSH_KEY,
                f"{worker_host['ssh_user']}@{worker_host['host']}",
                docker_cmd,
            ]
        else:
            cmd = ["sh", "-c", docker_cmd]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            return ""
        return (out or b"").decode(errors="replace").strip()

    # ── Session runner ───────────────────────────────────────────────

    async def _run_session(
        self,
        websocket: WebSocket,
        cmd: list[str],
        *,
        cleanup: dict | None = None,
    ) -> tuple[int, int]:
        """Spawn ``cmd`` in a PTY and bridge it to the WebSocket.

        Return ``(returncode, bytes_forwarded_to_ws)``. ``bytes=0`` means
        nothing has yet left the PTY toward the browser: the caller can use
        that information to decide whether to activate a fallback branch
        without polluting the visible stream.
        """
        master_fd, slave_fd = pty.openpty()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=os.setsid,
            )
        except Exception as exc:
            log.error("Console spawn failed for %s: %s", cmd[0], exc)
            await websocket.send_text(f"\r\n[Error starting console: {exc}]\r\n")
            os.close(master_fd)
            os.close(slave_fd)
            return -1, 0

        os.close(slave_fd)
        bytes_forwarded = 0
        try:
            bytes_forwarded = await self._bridge(websocket, master_fd, proc)
        finally:
            log.info("Console detach: pid=%d rc=%s", proc.pid, proc.returncode)
            await self._graceful_telnet_close(master_fd, proc, cmd)
            self._kill_process(proc)
            if cleanup:
                await self._cleanup_remote_telnet(cleanup)
            try:
                os.close(master_fd)
            except OSError:
                pass
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        return (proc.returncode if proc.returncode is not None else -1, bytes_forwarded)

    # ── Internals ────────────────────────────────────────────────────

    @staticmethod
    def _cleanup_spec(
        container: ContainerInfo,
        worker_host: dict | None,
        cmd: list[str],
    ) -> dict | None:
        joined = " ".join(cmd)
        for port in range(5000, 5008):
            if f"telnet 127.0.0.1 {port}" in joined:
                return {
                    "type": "container",
                    "container": container.name,
                    "worker_host": worker_host,
                    "port": port,
                }
        return None

    @staticmethod
    async def _graceful_telnet_close(
        master_fd: int,
        proc: asyncio.subprocess.Process,
        cmd: list[str],
    ) -> None:
        """Ask telnet sessions to exit before killing the PTY process group."""
        if proc.returncode is not None:
            return
        joined = " ".join(cmd)
        if "telnet" not in joined:
            return
        try:
            os.write(master_fd, b"\x1dquit\r")
        except OSError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    async def _cleanup_remote_telnet(cleanup: dict) -> None:
        """Best-effort cleanup for telnet left behind inside a VD container."""
        container = cleanup["container"]
        worker_host = cleanup.get("worker_host")
        port = int(cleanup["port"])
        pattern = f"[t]elnet 127[.]0[.]0[.]1 {port}"
        shell_expr = f"pkill -f {_shell_quote(pattern)} || true"
        docker_cmd = f"docker exec {container} sh -c {_shell_quote(shell_expr)}"
        if worker_host:
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ServerAliveInterval=30",
                "-o", "BatchMode=yes",
                "-i", settings.GUI_SSH_KEY,
                f"{worker_host['ssh_user']}@{worker_host['host']}",
                docker_cmd,
            ]
        else:
            cmd = ["sh", "-c", docker_cmd]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception as exc:
            log.debug("console cleanup failed for %s:%s: %s", container, port, exc)

    @staticmethod
    def _kill_process(proc: asyncio.subprocess.Process) -> None:
        """Kill process and its entire process group."""
        if proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    async def _bridge(
        websocket: WebSocket,
        master_fd: int,
        proc: asyncio.subprocess.Process,
    ) -> int:
        """Piping PTY ↔ WebSocket.

        Return the total number of bytes forwarded from the PTY to the WS,
        which lets the dispatcher know whether the session already produced
        browser-visible output before a possible rc=42.
        """
        loop = asyncio.get_event_loop()
        done = asyncio.Event()
        bytes_out = [0]  # list used for mutation from a closure

        pty_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def _on_pty_readable():
            try:
                data = os.read(master_fd, _READ_SIZE)
                if data:
                    pty_queue.put_nowait(data)
                else:
                    pty_queue.put_nowait(None)
            except OSError:
                pty_queue.put_nowait(None)

        loop.add_reader(master_fd, _on_pty_readable)

        async def read_pty():
            """Forward PTY output to WebSocket."""
            try:
                while not done.is_set():
                    try:
                        data = await asyncio.wait_for(pty_queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        if proc.returncode is not None:
                            break
                        continue
                    if data is None:
                        break
                    bytes_out[0] += len(data)
                    await websocket.send_bytes(data)
            finally:
                done.set()

        async def write_pty():
            """Forward WebSocket input to PTY."""
            try:
                while not done.is_set():
                    try:
                        msg = await asyncio.wait_for(
                            websocket.receive(), timeout=2.0
                        )
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        break
                    if msg["type"] == "websocket.disconnect":
                        break
                    payload = msg.get("bytes") or (msg.get("text") or "").encode()
                    if payload:
                        try:
                            os.write(master_fd, payload)
                        except OSError:
                            break
            finally:
                done.set()

        tasks = [
            asyncio.create_task(read_pty()),
            asyncio.create_task(write_pty()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            done.set()
            loop.remove_reader(master_fd)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return bytes_out[0]

    @staticmethod
    def resize(master_fd: int, rows: int, cols: int) -> None:
        """Resize the PTY window."""
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)


def _shell_quote(s: str) -> str:
    """Quoting POSIX-safe for passare un comando come singolo
    argomento a ``sh -c`` o a ``ssh remote`` (che a sua volta lo
    rilancia in una shell)."""
    return "'" + s.replace("'", "'\"'\"'") + "'"
