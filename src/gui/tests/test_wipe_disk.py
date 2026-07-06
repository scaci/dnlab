import json

from app.services.multinode_service import _drop_node_placement


def test_drop_node_placement_updates_wrapped_history(tmp_path):
    path = tmp_path / ".lab.placement.json"
    path.write_text(json.dumps({
        "lab": "lab",
        "placements": {
            "r1": "worker1",
            "r2": "worker2",
        },
    }))

    _drop_node_placement(path, "r1")

    data = json.loads(path.read_text())
    assert data["placements"] == {"r2": "worker2"}
    assert data["lab"] == "lab"


def test_drop_node_placement_updates_legacy_history(tmp_path):
    path = tmp_path / ".lab.placement.json"
    path.write_text(json.dumps({"r1": "worker1", "r2": "worker2"}))

    _drop_node_placement(path, "r1")

    assert json.loads(path.read_text()) == {"r2": "worker2"}
