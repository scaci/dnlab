"""GUI-local loader for shared dNLab filesystem paths.

The GUI reads the same ``/etc/dnlab/paths.yml`` file as dnlab-multinode, but it
should not import the orchestrator package just to resolve path defaults during
application startup.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import yaml


log = logging.getLogger(__name__)

DEFAULT_PATHS_FILE = "/etc/dnlab/paths.yml"

_DEFAULTS: dict[str, str] = {
    "hosts_file": "/etc/dnlab/hosts.yml",
    "image_sync_state": "/var/lib/dnlab-image-sync/state.json",
    "lab_cleanup_state": "/var/lib/dnlab-lab-cleanup/state.json",
    "persist_root": "/var/lib/docker/dnlab-backups",
    "topologies_dir": "/root/dnlab-topologies",
    "gui_dir": "/opt/dnlab-gui",
    "multinode_dir": "/opt/dnlab-multinode",
    "image_build_dir": "/opt/dnlab-image-build",
    "vrnetlab_dir": "/opt/vrnetlab",
    "image_build_workspace": "/var/lib/dnlab-image-build",
    "ssh_key": "/root/.ssh/id_ed25519",
    "gui_ssh_key": "/root/.ssh/dnlab-gui.key",
    "log_root": "/var/log/dnlab",
    "tmp_dir": "/tmp",
    "containerlab_bin": "/usr/bin/containerlab",
    "docker_socket": "unix:///var/run/docker.sock",
}


@dataclass(frozen=True)
class Paths:
    hosts_file: str
    image_sync_state: str
    lab_cleanup_state: str
    persist_root: str
    topologies_dir: str
    gui_dir: str
    multinode_dir: str
    image_build_dir: str
    vrnetlab_dir: str
    image_build_workspace: str
    ssh_key: str
    gui_ssh_key: str
    log_root: str
    tmp_dir: str
    containerlab_bin: str
    docker_socket: str


def _load() -> Paths:
    path = os.getenv("DNLAB_PATHS_FILE", DEFAULT_PATHS_FILE)
    data: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: expected mapping, got {type(raw).__name__}")
        data = {str(k): str(v) for k, v in raw.items() if v is not None}
    except FileNotFoundError:
        log.warning("Paths file %s not found - using built-in defaults", path)
    except Exception as exc:
        log.error("Failed to read %s: %s - using built-in defaults", path, exc)

    merged = {**_DEFAULTS, **data}
    extra = set(data) - set(_DEFAULTS)
    if extra:
        log.warning("Ignoring unknown keys in %s: %s", path, sorted(extra))
    return Paths(**{key: merged[key] for key in _DEFAULTS})


PATHS = _load()
