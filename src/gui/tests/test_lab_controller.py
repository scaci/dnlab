import asyncio
from pathlib import Path
from uuid import UUID

from app.controllers import lab_controller as lab_controller_mod
from app.controllers.lab_controller import LabController
from app.services.lab_resolver import ResolvedLab


def test_lab_status_preserves_node_apply_mode(monkeypatch, tmp_path: Path):
    async def fake_status(_lab, emit_events=False):
        return {
            "nodes": {
                "R1": {
                    "container": "clab-demo-R1",
                    "image": "alpine:3",
                    "kind": "linux",
                    "state": "running",
                    "mgmt_ipv4": "172.20.0.11",
                    "apply_mode": "live",
                }
            }
        }

    monkeypatch.setattr(
        lab_controller_mod.multinode,
        "status",
        fake_status,
    )

    lab = ResolvedLab(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        display_name="Demo Lab",
        netname="demo",
        bridge="br-demo",
        yaml_path=tmp_path / "demo.yml",
        owner=None,
    )

    result = asyncio.run(LabController().get_lab_status(lab))

    assert result is not None
    assert result.status == "running"
    assert result.containers[0].node_name == "R1"
    assert result.containers[0].apply_mode == "live"
    assert result.model_dump()["containers"][0]["apply_mode"] == "live"
