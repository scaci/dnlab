"""Test helper for importing the runtime relay executable module."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def load_runtime_relay_module():
    path = Path(__file__).resolve().parents[1] / "runtime-relay" / "relay.py"
    spec = importlib.util.spec_from_file_location("dnlab_runtime_relay_exe", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module
