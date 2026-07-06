"""Persistence backend helpers for VD overlay state.

Default behaviour is local-sticky: keep a small placement history next to the
topology and prefer the same worker for persistent VD overlays. When the
scheduler legitimately remaps a persistent VD, migrate its overlay before
containerlab deploy, but only while the lab is offline.

CephFS is treated as an explicit plugin backend. This module only validates
the shared mount and lets the existing /persist bind use that mountpoint.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import uuid
from pathlib import Path
from typing import Any

from dnlab_multinode.models.schedule import SchedulePlan
from dnlab_multinode.models.topology import DistributedTopology
from dnlab_multinode.services import generator, state as state_svc
from dnlab_multinode.services.paths import persist_dir_for_node
from dnlab_multinode.services.ssh import SSHClient
from dnlab_multinode.utils.naming import vd_container_name

log = logging.getLogger(__name__)


class PersistenceError(Exception):
    pass


def placement_file_path(lab_name: str, directory: Path = Path(".")) -> Path:
    return directory / f".{lab_name}.placement.json"


def load_placement_history(lab_name: str, directory: Path = Path(".")) -> dict[str, str]:
    path = placement_file_path(lab_name, directory)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        log.warning("Ignoring malformed placement history %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    placements = data.get("placements", data)
    if not isinstance(placements, dict):
        return {}
    return {str(k): str(v) for k, v in placements.items() if k and v}


def _persist_key(topo: DistributedTopology, vd_name: str) -> str:
    node = topo.nodes[vd_name]
    return str(node.persist_id or vd_name)


def load_placement_preferences(
    topo: DistributedTopology,
    directory: Path = Path("."),
) -> dict[str, str]:
    """Return scheduler preferences keyed by current VD name.

    The on-disk history is keyed by stable persist ID in the new format, but
    the scheduler operates on current topology node names. Legacy histories
    keyed by node name are accepted as a fallback.
    """
    history = load_placement_history(topo.name, directory)
    preferences: dict[str, str] = {}
    for vd_name in topo.nodes:
        host = history.get(_persist_key(topo, vd_name)) or history.get(vd_name)
        if host:
            preferences[vd_name] = host
    return preferences


def save_placement_history(
    topo: DistributedTopology,
    plan: SchedulePlan,
    directory: Path = Path("."),
) -> Path:
    placements: dict[str, str] = {}
    nodes: dict[str, str] = {}
    for vd_name, node in topo.nodes.items():
        if not generator._needs_persist_bind(node.image):
            continue
        host = plan.host_for_vd(vd_name)
        if host:
            key = _persist_key(topo, vd_name)
            placements[key] = host
            nodes[key] = vd_name
    path = placement_file_path(topo.name, directory)
    path.write_text(json.dumps({
        "lab": topo.name,
        "placements": placements,
        "nodes": nodes,
    }, indent=2))
    log.info("Persistence placement history saved: %s", path)
    return path


def prepare_persistence(
    topo: DistributedTopology,
    plan: SchedulePlan,
    clients: dict[str, SSHClient],
    directory: Path,
    progress: Any,
) -> None:
    """Prepare persistent overlays before containerlab deploy."""
    backend = topo.persistence.backend
    log.info("Persistence backend for %s: %s", topo.name, backend)

    if backend == "cephfs":
        try:
            _preflight_cephfs(topo, clients, progress)
            return
        except Exception:
            if not topo.persistence.allow_migration_fallback:
                raise
            log.warning(
                "CephFS preflight failed; falling back to local-sticky migration",
                exc_info=True,
            )
            progress.emit(
                "persistence",
                "info",
                detail="CephFS preflight failed; falling back to local-sticky overlay handling",
                data={"backend": "local-sticky"},
            )

    _prepare_local_sticky(topo, plan, clients, directory, progress)


def _persistent_vds(topo: DistributedTopology) -> list[str]:
    return [
        vd_name
        for vd_name, node in topo.nodes.items()
        if generator._needs_persist_bind(node.image)
    ]


def _prepare_local_sticky(
    topo: DistributedTopology,
    plan: SchedulePlan,
    clients: dict[str, SSHClient],
    directory: Path,
    progress: Any,
) -> None:
    persistent = _persistent_vds(topo)
    if not persistent:
        log.info("No persistent VD overlays required")
        return

    history = load_placement_history(topo.name, directory)
    migrations: list[tuple[str, str, str]] = []

    for vd_name in persistent:
        new_host = plan.host_for_vd(vd_name)
        if not new_host:
            continue
        _adopt_legacy_overlay_paths(topo, vd_name, clients)
        old_host = history.get(_persist_key(topo, vd_name)) or history.get(vd_name)
        if old_host and old_host != new_host:
            if old_host not in clients:
                raise PersistenceError(
                    f"Overlay for {vd_name} was last placed on {old_host}, "
                    "but that host is not in the current inventory"
                )
            if not _overlay_has_content(topo, vd_name, clients[old_host]):
                raise PersistenceError(
                    f"Overlay for {vd_name} was last placed on {old_host}, "
                    "but no overlay data was found there"
                )
            migrations.append((vd_name, old_host, new_host))
            continue

        found = _find_existing_overlay_hosts(topo, vd_name, clients)
        if len(found) > 1:
            raise PersistenceError(
                f"Overlay for {vd_name} exists on multiple hosts ({', '.join(found)}); "
                "manual reconciliation is required before deploy"
            )
        if found and found[0] != new_host:
            migrations.append((vd_name, found[0], new_host))

    if not migrations:
        log.info("Persistent overlays already match the current schedule")
        return

    _assert_lab_offline(topo, clients, directory)
    for vd_name, old_host, new_host in migrations:
        progress.emit(
            "overlay-migration",
            "start",
            host=new_host,
            detail=f"Migrating overlay {vd_name}: {old_host} -> {new_host}",
            data={"node": vd_name, "from": old_host, "to": new_host},
        )
        try:
            size = _migrate_overlay(topo, vd_name, clients[old_host], clients[new_host])
        except Exception as exc:
            progress.emit(
                "overlay-migration",
                "error",
                host=new_host,
                detail=f"Overlay migration failed for {vd_name}: {exc}",
                data={"node": vd_name, "from": old_host, "to": new_host},
            )
            raise
        progress.emit(
            "overlay-migration",
            "ok",
            host=new_host,
            detail=f"Overlay {vd_name} migrated from {old_host} to {new_host}",
            data={"node": vd_name, "from": old_host, "to": new_host, "bytes": size},
        )


def _find_existing_overlay_hosts(
    topo: DistributedTopology,
    vd_name: str,
    clients: dict[str, SSHClient],
) -> list[str]:
    node = topo.nodes[vd_name]
    path = persist_dir_for_node(topo.name, vd_name, node.persist_id, topo.persistence.root)
    found: list[str] = []
    for host_name, client in clients.items():
        rc, out, _err = client.run_no_check(
            f"test -d {shlex.quote(path)} && "
            f"find {shlex.quote(path)} -mindepth 1 -print -quit",
            timeout=15,
        )
        if rc == 0 and out.strip():
            found.append(host_name)
    return found


def _adopt_legacy_overlay_paths(
    topo: DistributedTopology,
    vd_name: str,
    clients: dict[str, SSHClient],
) -> None:
    """Move pre-ID overlay data from <lab>/<node> to <lab>/<persist_id>.

    This keeps old GUI topologies compatible when the first save adds the new
    node-id sidecar while existing disks still live under the node name.
    """
    node = topo.nodes[vd_name]
    if not node.persist_id or node.persist_id == vd_name:
        return
    legacy = persist_dir_for_node(topo.name, vd_name, "", topo.persistence.root)
    stable = persist_dir_for_node(topo.name, vd_name, node.persist_id, topo.persistence.root)
    if legacy == stable:
        return

    q_legacy = shlex.quote(legacy)
    q_stable = shlex.quote(stable)
    q_parent = shlex.quote(str(Path(stable).parent))
    cmd = (
        f"if test -d {q_legacy} && "
        f"find {q_legacy} -mindepth 1 -print -quit | grep -q .; then "
        f"if test -d {q_stable} && "
        f"find {q_stable} -mindepth 1 -print -quit | grep -q .; then "
        "exit 20; "
        "fi; "
        f"mkdir -p {q_parent}; "
        f"rm -rf -- {q_stable}; "
        f"mv -- {q_legacy} {q_stable}; "
        "fi"
    )
    for host_name, client in clients.items():
        rc, _out, err = client.run_no_check(cmd, timeout=60)
        if rc == 20:
            raise PersistenceError(
                f"Overlay for {vd_name} exists in both legacy path {legacy} "
                f"and stable path {stable} on {host_name}; manual reconciliation is required"
            )
        if rc != 0:
            raise PersistenceError(
                f"Cannot adopt legacy overlay for {vd_name} on {host_name}: "
                f"{err or f'rc={rc}'}"
            )


def _overlay_has_content(
    topo: DistributedTopology,
    vd_name: str,
    client: SSHClient,
) -> bool:
    node = topo.nodes[vd_name]
    path = persist_dir_for_node(topo.name, vd_name, node.persist_id, topo.persistence.root)
    rc, out, _err = client.run_no_check(
        f"test -d {shlex.quote(path)} && "
        f"find {shlex.quote(path)} -mindepth 1 -print -quit",
        timeout=15,
    )
    return rc == 0 and bool(out.strip())


def _assert_lab_offline(
    topo: DistributedTopology,
    clients: dict[str, SSHClient],
    directory: Path,
) -> None:
    state_path = state_svc.state_file_path(topo.name, directory)
    if state_path.exists():
        raise PersistenceError(
            f"Lab {topo.name} appears deployed ({state_path} exists); "
            "destroy it before migrating persistent overlays"
        )

    names = " ".join(
        shlex.quote(vd_container_name(topo.name, vd)) for vd in _persistent_vds(topo)
    )
    if not names:
        return
    cmd = (
        "for n in " + names + "; do "
        "docker ps --format '{{.Names}}' | grep -Fx \"$n\" && exit 10; "
        "done; exit 0"
    )
    for host_name, client in clients.items():
        rc, out, err = client.run_no_check(cmd, timeout=20)
        if rc == 10:
            running = (out or "").strip().splitlines()[0]
            raise PersistenceError(
                f"Lab {topo.name} is still running on {host_name} ({running}); "
                "destroy it before migrating persistent overlays"
            )
        if rc != 0:
            raise PersistenceError(
                f"Cannot verify lab offline status on {host_name}: rc={rc} {err}"
            )


def _migrate_overlay(
    topo: DistributedTopology,
    vd_name: str,
    src: SSHClient,
    dst: SSHClient,
) -> int:
    node = topo.nodes[vd_name]
    path = Path(persist_dir_for_node(topo.name, vd_name, node.persist_id, topo.persistence.root))
    parent = str(path.parent)
    leaf = path.name
    size = _remote_du_bytes(src, str(path))

    dst.run(f"mkdir -p {shlex.quote(parent)}", timeout=30)
    src_cmd = _ssh_command(src, f"tar -C {shlex.quote(parent)} -cpf - {shlex.quote(leaf)}")
    dst_cmd = _ssh_command(dst, f"tar -C {shlex.quote(parent)} -xpf -")

    log.info("Migrating overlay %s/%s: %s -> %s (%d bytes)",
             topo.name, vd_name, src.name, dst.name, size)
    src_proc = subprocess.Popen(src_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert src_proc.stdout is not None
    dst_proc = subprocess.Popen(dst_cmd, stdin=src_proc.stdout, stderr=subprocess.PIPE)
    src_proc.stdout.close()
    dst_err = dst_proc.communicate(timeout=3600)[1].decode(errors="replace").strip()
    src_err = src_proc.stderr.read().decode(errors="replace").strip()
    src_rc = src_proc.wait(timeout=30)

    if src_rc != 0:
        raise PersistenceError(f"source tar failed on {src.name}: {src_err}")
    if dst_proc.returncode != 0:
        raise PersistenceError(f"destination tar failed on {dst.name}: {dst_err}")

    rc, _out, err = src.run_no_check(
        f"rm -rf -- {shlex.quote(str(path))}",
        timeout=60,
    )
    if rc != 0:
        raise PersistenceError(
            f"overlay migrated to {dst.name}, but failed to remove source "
            f"on {src.name}: {err or f'rc={rc}'}"
        )
    return size


def _remote_du_bytes(client: SSHClient, path: str) -> int:
    out = client.run(
        f"du -sb {shlex.quote(path)} 2>/dev/null | awk '{{print $1}}'",
        timeout=60,
        check=False,
    )
    try:
        return int((out or "0").strip() or "0")
    except ValueError:
        return 0


def _ssh_command(client: SSHClient, remote_command: str) -> list[str]:
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-i", client.key_path,
        f"{client.user}@{client.host}",
        remote_command,
    ]
    return cmd


def _preflight_cephfs(
    topo: DistributedTopology,
    clients: dict[str, SSHClient],
    progress: Any,
) -> None:
    cfg = topo.persistence.cephfs
    mount = cfg.mountpoint.rstrip("/")
    expected = {x.strip() for x in cfg.expected_fstype.split(",") if x.strip()}

    progress.emit(
        "persistence",
        "info",
        detail=f"Checking CephFS persistence mount at {mount}",
        data={"backend": "cephfs", "mountpoint": mount},
    )

    for host_name, client in clients.items():
        client.run(f"test -d {shlex.quote(mount)}", timeout=15)
        if expected:
            fstype = client.run(
                f"stat -f -c %T {shlex.quote(mount)}",
                timeout=15,
            ).strip()
            if fstype not in expected:
                raise PersistenceError(
                    f"[{host_name}] {mount} filesystem type is {fstype!r}, "
                    f"expected one of {sorted(expected)}"
                )

    if not cfg.require_shared_marker:
        return

    token = f"dnlab-cephfs-{uuid.uuid4()}"
    marker = f"{mount}/{cfg.marker}"
    master = clients.get("master")
    if master is None:
        raise PersistenceError("CephFS shared marker check requires a master host")
    master.run(f"printf %s {shlex.quote(token)} > {shlex.quote(marker)}", timeout=15)
    try:
        for host_name, client in clients.items():
            seen = client.run(f"cat {shlex.quote(marker)}", timeout=15).strip()
            if seen != token:
                raise PersistenceError(
                    f"[{host_name}] CephFS marker mismatch at {marker}"
                )
    finally:
        master.run(f"rm -f {shlex.quote(marker)}", timeout=15, check=False)
