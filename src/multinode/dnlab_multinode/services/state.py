"""Deployment state persistence (JSON file)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from dnlab_multinode.models.state import DeploymentState

log = logging.getLogger(__name__)


def state_file_path(lab_name: str, directory: Path = Path(".")) -> Path:
    return directory / f".{lab_name}.multinode.json"


def save_state(state: DeploymentState, directory: Path = Path(".")) -> Path:
    path = state_file_path(state.lab_name, directory)
    data = state.to_dict()
    path.write_text(json.dumps(data, indent=2))
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
