"""Helpers for protecting persistent QEMU-backed VD disks.

Containerlab lifecycle commands ultimately stop Docker containers. For vrnetlab
images this can terminate the launcher before the guest OS has flushed its
filesystem, leaving a persistent qcow2 dirty or partially corrupted. dNLab owns
the persistence guarantee, so it asks QEMU for a guest powerdown before allowing
Containerlab to stop, restart, destroy, or recreate persistent VM containers.
"""

from __future__ import annotations

import logging
import shlex

from dnlab_multinode.services.ssh import SSHClient

log = logging.getLogger(__name__)


class GuestShutdownError(RuntimeError):
    pass


def image_uses_persistent_disk(image: str) -> bool:
    image = str(image or "").strip()
    if not image:
        return False
    if image.rsplit(":", 1)[-1].endswith("-dnlab"):
        return True
    repo = image.rsplit(":", 1)[0] if ":" in image else image
    return repo.rsplit("/", 1)[-1].startswith("dnlab_")


def graceful_powerdown_container(
    client: SSHClient,
    container: str,
    *,
    timeout: int = 15,
) -> None:
    """Ask a running QEMU guest to power down and wait for QEMU to exit.

    Non-QEMU containers are treated as a no-op. The preferred path is the QEMU
    monitor ``system_powerdown`` command. If the monitor is unavailable, fall
    back to ``docker stop --time`` so a lab destroy is not blocked by a stale
    or kind-specific monitor endpoint.
    """
    container = str(container or "").strip()
    if not container:
        return

    quoted = shlex.quote(container)
    timeout = max(5, int(timeout))
    monitor_code = (
        "import socket, sys\n"
        "last = None\n"
        "for host in ('127.0.0.1', '::1'):\n"
        "    try:\n"
        "        s = socket.create_connection((host, 4000), 2)\n"
        "        break\n"
        "    except OSError as exc:\n"
        "        last = exc\n"
        "else:\n"
        "    print(last or 'qemu monitor unavailable', file=sys.stderr)\n"
        "    sys.exit(3)\n"
        "s.settimeout(1)\n"
        "try:\n"
        "    s.recv(4096)\n"
        "except Exception:\n"
        "    pass\n"
        "s.sendall(b'system_powerdown\\n')\n"
        "try:\n"
        "    s.recv(4096)\n"
        "except Exception:\n"
        "    pass\n"
        "s.close()\n"
    )
    quoted_monitor_code = shlex.quote(monitor_code)
    monitor_cmd = (
        "py=$(command -v python3 || command -v python || "
        "command -v /.venv/bin/python3 || command -v /.venv/bin/python); "
        "if [ -z \"$py\" ]; then echo 'python unavailable for qemu monitor' >&2; exit 127; fi; "
        f"exec \"$py\" -c {quoted_monitor_code}"
    )
    cmd = (
        f"if ! docker exec {quoted} sh -lc "
        f"{shlex.quote('pgrep -f \"^qemu-system\" >/dev/null')} "
        "2>/dev/null; then exit 0; fi; "
        f"docker exec {quoted} sh -lc {shlex.quote(monitor_cmd)} || exit $?; "
        f"for i in $(seq 1 {timeout}); do "
        f"running=$(docker inspect -f '{{{{.State.Running}}}}' {quoted} 2>/dev/null || echo false); "
        "if [ \"$running\" != true ]; then exit 0; fi; "
        f"if ! docker exec {quoted} sh -lc "
        f"{shlex.quote('pgrep -f \"^qemu-system\" >/dev/null')} "
        "2>/dev/null; then exit 0; fi; "
        "sleep 1; "
        "done; "
        "exit 124"
    )
    rc, _out, err = client.run_no_check(cmd, timeout=timeout + 15)
    if rc == 0:
        log.info("[%s] QEMU guest powered down cleanly: %s", client.name, container)
        return
    if rc == 3:
        log.warning(
            "[%s] QEMU monitor unavailable for %s; falling back to docker stop",
            client.name,
            container,
        )
        _docker_stop_container(client, container, timeout=timeout)
        return
    if rc == 127:
        log.warning(
            "[%s] monitor helper unavailable for %s; falling back to docker stop",
            client.name,
            container,
        )
        _docker_stop_container(client, container, timeout=timeout)
        return
    if rc == 124:
        log.warning(
            "[%s] QEMU guest did not power down within %ss for %s; "
            "falling back to docker stop",
            client.name,
            timeout,
            container,
        )
        _docker_stop_container(client, container, timeout=timeout)
        return
    raise GuestShutdownError(
        f"{container}: guest powerdown failed"
        + (f": {err}" if err else f" (rc={rc})")
    )


def _docker_stop_container(
    client: SSHClient,
    container: str,
    *,
    timeout: int,
) -> None:
    quoted = shlex.quote(container)
    rc, _out, err = client.run_no_check(
        f"docker stop --time {int(timeout)} {quoted} >/dev/null; "
        f"running=$(docker inspect -f '{{{{.State.Running}}}}' {quoted} 2>/dev/null || echo false); "
        "test \"$running\" != true",
        timeout=timeout + 15,
    )
    if rc == 0:
        log.info("[%s] container stopped via docker fallback: %s", client.name, container)
        return
    raise GuestShutdownError(
        f"{container}: docker stop fallback failed"
        + (f": {err}" if err else f" (rc={rc})")
    )
