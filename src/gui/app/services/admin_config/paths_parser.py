"""Structured parser for /etc/dnlab/paths.yml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .base import ConfigParseError, dump_yaml, load_yaml_mapping, reject_empty


PATH_FIELDS = [
    "containerlab_bin",
    "docker_socket",
    "topologies_dir",
    "gui_dir",
    "multinode_dir",
    "image_build_dir",
    "vrnetlab_dir",
    "image_build_workspace",
    "hosts_file",
    "persist_root",
    "ssh_key",
    "gui_ssh_key",
    "image_sync_state",
    "lab_cleanup_state",
    "log_dir_multinode",
    "log_dir_gui",
    "tmp_dir",
]

RESTART_FIELDS = set(PATH_FIELDS)

_MULTINODE_MANAGED_FIELDS = {
    "containerlab_bin",
    "docker_socket",
    "multinode_dir",
    "log_dir_multinode",
}

_IMAGE_BUILD_MANAGED_FIELDS = {
    "image_build_dir",
    "vrnetlab_dir",
    "image_build_workspace",
}

_DAEMON_STATE_FIELDS = {
    "image_sync_state",
    "lab_cleanup_state",
}


class PathEntry(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=4096)
    known: bool = True
    exists: bool | None = None
    warning: str | None = None
    scope: str = "local"
    status_label: str = "ok"


class PathsConfigData(BaseModel):
    entries: list[PathEntry]
    extra: dict[str, Any] = {}
    restart_required: bool = True


class PathsConfigModel(BaseModel):
    key: str = "paths"
    path: str
    exists: bool
    data: PathsConfigData
    warnings: list[str] = []


def parse_paths_config(content: str, path: Path, exists: bool) -> PathsConfigModel:
    raw = load_yaml_mapping(content, "paths")
    entries: list[PathEntry] = []
    warnings: list[str] = []
    seen = set()

    for key in PATH_FIELDS:
        if key in raw:
            value = "" if raw[key] is None else str(raw[key])
            entry = _entry(key, value, True)
            entries.append(entry)
            seen.add(key)
            if entry.warning:
                warnings.append(f"{key}: {entry.warning}")

    for key, value in raw.items():
        if key in seen:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            entry = _entry(key, "" if value is None else str(value), False)
            entries.append(entry)
            if entry.warning:
                warnings.append(f"{key}: {entry.warning}")

    extra = {k: v for k, v in raw.items() if k not in {e.key for e in entries}}
    return PathsConfigModel(
        path=str(path),
        exists=exists,
        data=PathsConfigData(entries=entries, extra=extra),
        warnings=warnings,
    )


def serialize_paths_config(model: PathsConfigModel) -> str:
    data: dict[str, Any] = {}
    keys: set[str] = set()
    for entry in model.data.entries:
        key = reject_empty(entry.key, "path key")
        if key in keys:
            raise ConfigParseError(f"duplicate path key: {key}")
        keys.add(key)
        value = reject_empty(entry.value, key)
        _validate_path_value(key, value)
        data[key] = value
    data.update(model.data.extra or {})
    return dump_yaml(data)


def _entry(key: str, value: str, known: bool) -> PathEntry:
    scope = _path_scope(key, known)
    exists = _path_exists(key, value)
    warning = _path_warning(key, value, exists)
    return PathEntry(
        key=key,
        value=value,
        known=known,
        exists=exists,
        warning=warning,
        scope=scope,
        status_label=_status_label(key, exists, warning),
    )


def _validate_path_value(key: str, value: str) -> None:
    if key == "docker_socket":
        if not (value.startswith("unix://") or value.startswith("tcp://") or value.startswith("/")):
            raise ConfigParseError("docker_socket must be a unix://, tcp://, or absolute path value")
        return
    if key.endswith("_bin") or key.endswith("_dir") or key.endswith("_file") or key.endswith("_key") or key in {
        "topologies_dir",
        "persist_root",
        "image_sync_state",
        "lab_cleanup_state",
        "image_build_workspace",
        "log_dir_multinode",
        "log_dir_gui",
        "tmp_dir",
    }:
        if not value.startswith("/"):
            raise ConfigParseError(f"{key} must be an absolute path")


def _path_warning(key: str, value: str, exists: bool | None = None) -> str | None:
    if not value:
        return "empty value"
    try:
        _validate_path_value(key, value)
    except ConfigParseError as exc:
        return str(exc)
    if exists is False:
        if _is_docker_managed_path(key):
            return None
        return "path does not exist yet"
    return None


def _path_exists(key: str, value: str) -> bool | None:
    if not value or value.startswith("tcp://"):
        return None
    candidate = value.removeprefix("unix://")
    if key == "docker_socket":
        return Path(candidate).exists()
    if key.endswith("_dir") or key in {
        "topologies_dir",
        "persist_root",
        "image_build_workspace",
        "log_dir_multinode",
        "log_dir_gui",
        "tmp_dir",
    }:
        return Path(candidate).is_dir()
    if key.endswith("_bin") or key.endswith("_file") or key.endswith("_key") or key in {
        "image_sync_state",
        "lab_cleanup_state",
    }:
        return Path(candidate).exists()
    return None


def _path_scope(key: str, known: bool) -> str:
    if not known:
        return "custom"
    if key in _MULTINODE_MANAGED_FIELDS:
        return "dnlab-multinode"
    if key in _IMAGE_BUILD_MANAGED_FIELDS:
        return "dnlab-image-build"
    if key in _DAEMON_STATE_FIELDS:
        return "daemon-state"
    return "local"


def _status_label(key: str, exists: bool | None, warning: str | None) -> str:
    if warning:
        return warning
    if _docker_target_enabled():
        if key in _MULTINODE_MANAGED_FIELDS:
            return "managed by dnlab-multinode"
        if key in _IMAGE_BUILD_MANAGED_FIELDS:
            return "managed by dnlab-image-build"
        if key in _DAEMON_STATE_FIELDS:
            return "state generated by daemon"
    if exists is False:
        return "missing"
    return "ok"


def _is_docker_managed_path(key: str) -> bool:
    return _docker_target_enabled() and key in (
        _MULTINODE_MANAGED_FIELDS | _IMAGE_BUILD_MANAGED_FIELDS | _DAEMON_STATE_FIELDS
    )


def _docker_target_enabled() -> bool:
    return bool(os.getenv("DNLAB_MULTINODE_API_URL") or os.getenv("DNLAB_IMAGE_BUILD_API_URL"))
