#!/usr/bin/env python3
"""Small lab-scoped runtime relay for dNLab jumphost and GUI.

The relay is intentionally narrow: it accepts CONNECT and LOG requests for
containers explicitly allowlisted at deploy time, guarded by one lab-scoped
API key. It is the only component in this path with the Docker socket.
"""

from __future__ import annotations

import os
import pty
import select
import shlex
import signal
import socket
import subprocess
import sys
import threading
from hmac import compare_digest


HOST = os.getenv("RELAY_BIND", "0.0.0.0")
PORT = int(os.getenv("RELAY_PORT", "23000"))
API_KEY = os.getenv("RELAY_API_KEY", "")
ALLOWED = {x for x in os.getenv("RELAY_ALLOWED_CONTAINERS", "").split() if x}
READ_SIZE = 4096


def _send_line(sock: socket.socket, line: str) -> None:
    sock.sendall((line.rstrip("\n") + "\n").encode())


def _read_request(sock: socket.socket) -> list[str] | None:
    buf = b""
    while b"\n" not in buf and len(buf) < 8192:
        chunk = sock.recv(1024)
        if not chunk:
            return None
        buf += chunk
    line = buf.split(b"\n", 1)[0].decode(errors="replace").strip()
    return shlex.split(line) if line else None


def _authorized(key: str, container: str) -> bool:
    return bool(API_KEY) and compare_digest(key, API_KEY) and container in ALLOWED


def _container_running(container: str) -> bool:
    proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _discover_console_port(container: str) -> str:
    expr = (
        "ss -Htln 2>/dev/null | "
        "awk '$1==\"LISTEN\" && $4 !~ /^127\\.0\\.0\\.11:/ "
        "&& ($4 ~ /^127\\.0\\.0\\.1:/ || $4 ~ /^\\[::1\\]:/ "
        "|| $4 ~ /^\\*:/ || $4 ~ /^0\\.0\\.0\\.0:/ || $4 ~ /^\\[::\\]:/) "
        "{ n=split($4,a,\":\"); if (a[n] ~ /^500[0-7]$/) { print a[n]; exit } }'"
    )
    proc = subprocess.run(
        ["docker", "exec", container, "sh", "-c", expr],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _connect_cmd(container: str) -> tuple[list[str], str | None]:
    port = _discover_console_port(container)
    if port:
        return (
            ["docker", "exec", "-it", container, "telnet", "127.0.0.1", port],
            port,
        )
    # Native containers such as FRR do not expose a QEMU serial port.
    return (
        [
            "docker",
            "exec",
            "-it",
            container,
            "sh",
            "-lc",
            "command -v vtysh >/dev/null 2>&1 && exec vtysh || exec sh",
        ],
        None,
    )


def _cleanup_container_telnet(container: str, port: str | None) -> None:
    if not port:
        return
    if port not in {str(p) for p in range(5000, 5008)}:
        return
    pattern = f"[t]elnet 127[.]0[.]0[.]1 {port}"
    try:
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-c",
                f"pkill -f {shlex.quote(pattern)} || true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except Exception:
        pass


def _relay_pty(sock: socket.socket, cmd: list[str], container: str, port: str | None) -> None:
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        preexec_fn=os.setsid,
    )
    os.close(slave_fd)
    try:
        while proc.poll() is None:
            r, _, _ = select.select([sock, master_fd], [], [], 0.2)
            if sock in r:
                data = sock.recv(READ_SIZE)
                if not data:
                    break
                os.write(master_fd, data)
            if master_fd in r:
                try:
                    data = os.read(master_fd, READ_SIZE)
                except OSError:
                    break
                if not data:
                    break
                sock.sendall(data)
    finally:
        try:
            os.write(master_fd, b"\x1dquit\r")
        except OSError:
            pass
        if proc.poll() is None:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        _cleanup_container_telnet(container, port)


def _stream_logs(sock: socket.socket, container: str, tail: str, follow: str) -> None:
    args = ["docker", "logs"]
    if follow == "1":
        args.append("-f")
    if tail == "all":
        pass
    else:
        args.append(f"--tail={int(tail)}")
    args.append(container)
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    try:
        _send_line(sock, "OK")
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        while True:
            if proc.poll() is not None:
                remaining = os.read(fd, READ_SIZE)
                if remaining:
                    sock.sendall(remaining)
                break
            readable, _, _ = select.select([fd, sock], [], [], 0.2)
            if not readable:
                continue
            if sock in readable:
                try:
                    probe = sock.recv(1, socket.MSG_PEEK)
                except (BlockingIOError, InterruptedError):
                    probe = b"x"
                except OSError:
                    break
                if not probe:
                    break
            if fd in readable:
                data = os.read(fd, READ_SIZE)
                if not data:
                    break
                try:
                    sock.sendall(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()


def _handle(sock: socket.socket, _addr) -> None:
    with sock:
        try:
            parts = _read_request(sock)
            if not parts or len(parts) < 3:
                _send_line(sock, "ERR bad request")
                return
            action, key, container = parts[0], parts[1], parts[2]
            if not _authorized(key, container):
                _send_line(sock, "ERR unauthorized")
                return
            if not _container_running(container):
                _send_line(sock, "ERR container not running")
                return
            if action == "CONNECT":
                cmd, port = _connect_cmd(container)
                _relay_pty(sock, cmd, container, port)
                return
            if action == "LOG":
                tail = parts[3] if len(parts) > 3 else "200"
                follow = parts[4] if len(parts) > 4 else "0"
                _stream_logs(sock, container, tail, follow)
                return
            _send_line(sock, "ERR unknown action")
        except Exception as exc:
            try:
                _send_line(sock, f"ERR {exc}")
            except Exception:
                pass


def main() -> int:
    if not API_KEY:
        print("RELAY_API_KEY is required", file=sys.stderr)
        return 2
    if not ALLOWED:
        print("RELAY_ALLOWED_CONTAINERS is empty", file=sys.stderr)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(64)
    print(f"dnlab-runtime-relay listening on {HOST}:{PORT} for {len(ALLOWED)} containers", flush=True)
    while True:
        client, addr = srv.accept()
        threading.Thread(target=_handle, args=(client, addr), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
