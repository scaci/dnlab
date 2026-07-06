import yaml

from app.models.node import Node
from app.models.topology import Topology
from app.services.containerlab_service import ContainerLabService


def test_gui_kind_alias_is_saved_as_deploy_kind_with_sidecar(tmp_path):
    topo = Topology(
        name="lab",
        nodes=[
            Node(
                name="apstra1",
                kind="juniper_apstra",
                image="vrnetlab/juniper_apstra:6.1.2-28",
            )
        ],
    )
    path = tmp_path / "lab.yml"

    ContainerLabService().save_topology_to(path, topo)

    text = path.read_text()
    data = yaml.safe_load(text)
    node = data["topology"]["nodes"]["apstra1"]
    assert node["kind"] == "generic_vm"
    assert "# dnlab-gui-kinds:" in text
    assert '"apstra1": "juniper_apstra"' in text


def test_gui_kind_alias_is_restored_from_image_pattern(tmp_path):
    path = tmp_path / "lab.yml"
    path.write_text(
        """
name: lab
topology:
  nodes:
    apstra1:
      kind: generic_vm
      image: vrnetlab/juniper_apstra:6.1.2-28
  links: []
"""
    )

    topo = ContainerLabService().load_topology_from_file(path)

    node = topo.get_node("apstra1")
    assert node is not None
    assert node.kind == "juniper_apstra"


def test_mgmt_passthrough_is_not_globally_injected_for_unknown_kind(tmp_path):
    topo = Topology(
        name="lab",
        nodes=[
            Node(
                name="r1",
                kind="unknown_kind",
                image="alpine:latest",
            )
        ],
    )
    path = tmp_path / "lab.yml"

    ContainerLabService().save_topology_to(path, topo)

    data = yaml.safe_load(path.read_text())
    node = data["topology"]["nodes"]["r1"]
    assert "env" not in node


def test_catalog_default_mgmt_passthrough_is_injected(tmp_path):
    topo = Topology(
        name="lab",
        nodes=[
            Node(
                name="r1",
                kind="linux",
                image="alpine:latest",
            )
        ],
    )
    path = tmp_path / "lab.yml"

    ContainerLabService().save_topology_to(path, topo)

    data = yaml.safe_load(path.read_text())
    node = data["topology"]["nodes"]["r1"]
    assert node["env"]["CLAB_MGMT_PASSTHROUGH"] == "true"


def test_mgmt_passthrough_can_be_enabled_per_node(tmp_path):
    topo = Topology(
        name="lab",
        nodes=[
            Node(
                name="r1",
                kind="linux",
                image="alpine:latest",
                extra={"env": {"CLAB_MGMT_PASSTHROUGH": "true"}},
            )
        ],
    )
    path = tmp_path / "lab.yml"

    ContainerLabService().save_topology_to(path, topo)

    data = yaml.safe_load(path.read_text())
    node = data["topology"]["nodes"]["r1"]
    assert node["env"]["CLAB_MGMT_PASSTHROUGH"] == "true"
