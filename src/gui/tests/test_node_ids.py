import json
import uuid

from app.controllers.topology_controller import TopologyController
from app.models.node import Node
from app.models.topology import Topology
from app.services.containerlab_service import ContainerLabService


def _node_ids(text: str) -> dict[str, str]:
    prefix = "# dnlab-gui-node-ids: "
    line = next(l for l in text.splitlines() if l.startswith(prefix))
    return json.loads(line[len(prefix):])


def test_new_node_gets_stable_node_id_sidecar(tmp_path):
    path = tmp_path / "lab.yml"
    ctrl = TopologyController()
    ctrl._clab.save_topology_to(path, Topology(name="lab"))

    ctrl.add_node_by_path(
        path,
        "lab",
        Node(name="r1", kind="linux", image="quay.io/frrouting/frr:10.2.6-dnlab"),
    )

    ids = _node_ids(path.read_text())
    assert set(ids) == {"r1"}
    uuid.UUID(ids["r1"])


def test_rename_preserves_stable_node_id(tmp_path):
    path = tmp_path / "lab.yml"
    ctrl = TopologyController()
    ctrl._clab.save_topology_to(
        path,
        Topology(
            name="lab",
            nodes=[Node(name="r1", kind="linux", image="alpine:latest")],
            gui_node_ids_state={"r1": "11111111-1111-4111-8111-111111111111"},
        ),
    )

    ctrl.update_node_by_path(path, "lab", "r1", {"new_name": "r2"})

    ids = _node_ids(path.read_text())
    assert ids == {"r2": "11111111-1111-4111-8111-111111111111"}


def test_load_without_sidecar_generates_ids_for_vds_only(tmp_path):
    path = tmp_path / "lab.yml"
    path.write_text(
        """
name: lab
topology:
  nodes:
    r1:
      kind: linux
      image: alpine:latest
    real_net1:
      kind: _real_net
      image: ""
      extra:
        ipv4: 192.168.1.1/24
  links: []
"""
    )

    topo = ContainerLabService().load_topology_from_file(path)

    assert set(topo.gui_node_ids_state) == {"r1"}
    uuid.UUID(topo.gui_node_ids_state["r1"])
