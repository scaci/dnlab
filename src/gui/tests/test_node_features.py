import json

import yaml

from app.controllers.topology_controller import TopologyController
from app.models.node import Node
from app.models.topology import Topology


def test_node_features_are_written_as_gui_sidecar(tmp_path):
    path = tmp_path / "lab.yml"
    ctrl = TopologyController()
    ctrl._clab.save_topology_to(
        path,
        Topology(
            name="lab",
            nodes=[Node(name="r1", kind="frr", image="quay.io/frrouting/frr:10.2.6")],
        ),
    )

    ctrl.update_node_by_path(
        path,
        "lab",
        "r1",
        {
            "node_features": {
                "frr_daemons": {
                    "bgpd": False,
                    "ospfd": True,
                    "not-a-daemon": True,
                },
                "unknown_feature": {"x": True},
            },
        },
    )

    text = path.read_text()
    data = yaml.safe_load(text)
    node = data["topology"]["nodes"]["r1"]
    assert "node_features" not in node
    assert "frr_daemons" not in node

    sidecar_line = next(
        line for line in text.splitlines()
        if line.startswith("# dnlab-gui-node-features: ")
    )
    sidecar = json.loads(sidecar_line.removeprefix("# dnlab-gui-node-features: "))
    assert sidecar["r1"]["frr_daemons"]["state"] == {
        "bgpd": False,
        "ospfd": True,
    }
    assert sidecar["r1"]["frr_daemons"]["materialize"] == {
        "type": "persist-key-value-bool-file",
        "path": "frr/daemons",
        "true": "yes",
        "false": "no",
    }

    loaded = ctrl.get_by_path(path)
    assert loaded is not None
    assert loaded.gui_node_features_state == {
        "r1": {
            "frr_daemons": {
                "bgpd": False,
                "ospfd": True,
            },
        },
    }


def test_node_features_are_removed_when_kind_no_longer_enables_feature(tmp_path):
    path = tmp_path / "lab.yml"
    ctrl = TopologyController()
    ctrl._clab.save_topology_to(
        path,
        Topology(
            name="lab",
            nodes=[Node(name="r1", kind="nokia_srlinux", image="ghcr.io/nokia/srlinux:latest")],
        ),
    )

    ctrl.update_node_by_path(
        path,
        "lab",
        "r1",
        {"node_features": {"frr_daemons": {"bgpd": True}}},
    )

    assert "# dnlab-gui-node-features:" not in path.read_text()
