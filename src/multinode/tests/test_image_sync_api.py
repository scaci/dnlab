"""Tests for the image-sync HTTP control surface.

Skipped where fastapi/httpx are not installed (the daemon runs inside the
dnlab-multinode container image, which ships both).
"""

import json
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from dnlab_multinode.image_sync_api import build_app  # noqa: E402


class _StubDaemon:
    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file
        self.triggered = False

    def trigger_reconcile(self) -> None:
        self.triggered = True


def test_status_reports_state_when_present():
    state_file = Path(tempfile.mkdtemp()) / "state.json"
    state_file.write_text(json.dumps({"updated_at": "x", "reconcile_count": 2}))
    client = TestClient(build_app(_StubDaemon(state_file)))

    assert client.get("/health").json() == {"ok": True}
    body = client.get("/status").json()
    assert body["available"] is True
    assert body["state"]["reconcile_count"] == 2


def test_status_unavailable_when_state_missing():
    client = TestClient(build_app(_StubDaemon(Path("/nonexistent/state.json"))))
    assert client.get("/status").json() == {"available": False}


def test_reconcile_triggers_daemon():
    daemon = _StubDaemon(Path("/nonexistent/state.json"))
    client = TestClient(build_app(daemon))
    assert client.post("/reconcile").json() == {"triggered": True}
    assert daemon.triggered is True
