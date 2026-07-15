"""Deployment state persistence (JSON file)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from dnlab_multinode.models.state import DeploymentState

log = logging.getLogger(__name__)


def state_file_path(lab_name: str, directory: Path = Path(".")) -> Path:
    return directory / f".{lab_name}.multinode.json"


def save_state(state: DeploymentState, directory: Path = Path(".")) -> Path:
    path = state_file_path(state.lab_name, directory)
    data = state.to_dict()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    log.info("State saved: %s", path)
    return path


def load_state(lab_name: str, directory: Path = Path(".")) -> DeploymentState | None:
    path = state_file_path(lab_name, directory)
    if not path.exists():
        log.debug("No state file: %s", path)
        return None

    try:
        data = json.loads(path.read_text())
        state = DeploymentState.from_dict(data)
        log.info("State loaded: %s", path)
        return state
    except Exception as e:
        log.error("Failed to load state from %s: %s", path, e)
        return None


def delete_state(lab_name: str, directory: Path = Path(".")) -> None:
    path = state_file_path(lab_name, directory)
    if path.exists():
        path.unlink()
        log.info("State deleted: %s", path)
