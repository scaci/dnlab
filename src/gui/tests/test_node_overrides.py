import json
import re

import yaml

from app.controllers.topology_controller import TopologyController
from app.models.node import Node
from app.models.topology import Topology
from app.services.containerlab_service import ContainerLabService


def test_cat9kv_add_generates_vswitch_bind_and_sidecar(tmp_path):
    path = tmp_path / "lab.yml"
    ctrl = TopologyController()
    ctrl._clab.save_topology_to(path, Topology(name="lab"))

    ctrl.add_node_by_path(
        path,
        "lab",
        Node(name="cat1", kind="cisco_cat9kv", image="vrnetlab/cisco_cat9kv_v2:17.15.03"),
    )

    data = yaml.safe_load(path.read_text())
    node = data["topology"]["nodes"]["cat1"]
    assert node["binds"] == [f"{tmp_path}/node-assets/lab/cat1/vswitch.xml:/vswitch.xml"]

    vswitch = (tmp_path / "node-assets/lab/cat1/vswitch.xml").read_text()
    assert "<asic_type>UADP</asic_type>" in vswitch
    assert "<port_count>24</port_count>" in vswitch
    assert re.search(r"<serial_number>[A-Z0-9]{11,12}</serial_number>", vswitch)
    assert re.search(r"<prod_serial_number>[A-Z0-9]{11,12}</prod_serial_number>", vswitch)

    sidecar = _sidecar(path.read_text())
    assert sidecar["cat1"]["type"] == "cat9kv_vswitch"
    assert sidecar["cat1"]["platform"] == "UADP"


def test_cat9kv_update_writes_requested_platform_and_serial(tmp_path):
    path = tmp_path / "lab.yml"
    ctrl = TopologyController()
    ctrl._clab.save_topology_to(
        path,
        Topology(
            name="lab",
            nodes=[Node(name="cat1", kind="cisco_cat9kv", image="vrnetlab/cisco_cat9kv_v2:17.15.03")],
        ),
    )

    ctrl.update_node_by_path(
        path,
        "lab",
        "cat1",
        {
            "node_overrides": {
                "type": "cat9kv_vswitch",
                "platform": "Q200",
                "port_count": 24,
                "serial_number": "FOC12345678",
            }
        },
    )

    vswitch = (tmp_path / "node-assets/lab/cat1/vswitch.xml").read_text()
    assert "<asic_type>Q200</asic_type>" in vswitch
    assert "<serial_number>FOC12345678</serial_number>" in vswitch
    assert "<prod_serial_number>FOC12345678</prod_serial_number>" in vswitch

    topo = ContainerLabService().load_topology_from_file(path)
    assert topo.gui_node_overrides_state["cat1"]["platform"] == "Q200"


def test_stale_cat9kv_override_is_removed_when_kind_no_longer_applies(tmp_path):
    path = tmp_path / "lab.yml"
    ctrl = TopologyController()
    ctrl._clab.save_topology_to(
        path,
        Topology(
            name="lab",
            nodes=[
                Node(
                    name="wlc1",
                    kind="cisco_c9800cl",
                    image="vrnetlab/cisco_c9800cl:17.12.1",
                    extra={"binds": ["/tmp/old/vswitch.xml:/vswitch.xml"]},
                )
            ],
        ),
    )

    ctrl.update_node_by_path(
        path,
        "lab",
        "wlc1",
        {
            "node_overrides": {
                "type": "cat9kv_vswitch",
                "platform": "Q200",
                "port_count": 24,
                "serial_number": "FOC12345678",
            }
        },
    )

    topo = ContainerLabService().load_topology_from_file(path)
    assert "wlc1" not in topo.gui_node_overrides_state
    node = topo.get_node("wlc1")
    assert node is not None
    assert "binds" not in node.extra


def _sidecar(text):
    prefix = "# dnlab-gui-node-overrides: "
    line = next(l for l in text.splitlines() if l.startswith(prefix))
    return json.loads(line[len(prefix):])
