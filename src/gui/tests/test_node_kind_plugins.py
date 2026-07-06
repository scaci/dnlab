import yaml

from app.models.node import Node
from app.models.topology import Topology
from app.services.containerlab_service import ContainerLabService


def test_c9800_legacy_sparse_data_links_are_migrated(tmp_path):
    path = tmp_path / "lab.yml"
    path.write_text(
        """
name: lab
topology:
  nodes:
    wlc1:
      kind: cisco_cat9kv
      image: vrnetlab/cisco_c9800cl:17.12.1
    sw1:
      kind: linux
      image: alpine:latest
  links:
    - endpoints: ["wlc1:eth2", "sw1:eth1"]
    - endpoints: ["wlc1:eth3", "sw1:eth2"]
    - endpoints: ["wlc1:eth4", "sw1:eth3"]
"""
    )

    topo = ContainerLabService().load_topology_from_file(path)

    node = topo.get_node("wlc1")
    assert node is not None
    assert node.kind == "cisco_c9800cl"
    assert [link.source_iface for link in topo.links] == ["eth1", "eth2", "eth3"]


def test_c9800_migration_is_idempotent_for_correct_links(tmp_path):
    path = tmp_path / "lab.yml"
    path.write_text(
        """
name: lab
topology:
  nodes:
    wlc1:
      kind: cisco_cat9kv
      image: vrnetlab/cisco_c9800cl:17.12.1
    sw1:
      kind: linux
      image: alpine:latest
  links:
    - endpoints: ["wlc1:eth1", "sw1:eth1"]
    - endpoints: ["wlc1:eth2", "sw1:eth2"]
"""
    )

    topo = ContainerLabService().load_topology_from_file(path)

    assert [link.source_iface for link in topo.links] == ["eth1", "eth2"]


def test_c9800_migration_does_not_touch_non_c9800_cat9kv(tmp_path):
    path = tmp_path / "lab.yml"
    path.write_text(
        """
name: lab
topology:
  nodes:
    cat1:
      kind: cisco_cat9kv
      image: vrnetlab/cisco_cat9kv_v2:17.15.03
    sw1:
      kind: linux
      image: alpine:latest
  links:
    - endpoints: ["cat1:eth2", "sw1:eth1"]
    - endpoints: ["cat1:eth3", "sw1:eth2"]
"""
    )

    topo = ContainerLabService().load_topology_from_file(path)

    assert topo.get_node("cat1").kind == "cisco_cat9kv"
    assert [link.source_iface for link in topo.links] == ["eth2", "eth3"]


def test_c9800_does_not_inherit_cat9kv_override_from_deploy_kind(tmp_path):
    path = tmp_path / "lab.yml"
    path.write_text(
        """
name: lab
topology:
  nodes:
    wlc1:
      kind: cisco_cat9kv
      image: vrnetlab/cisco_c9800cl:17.12.1
  links: []
"""
    )

    topo = ContainerLabService().load_topology_from_file(path)
    ContainerLabService().save_topology_to(path, topo)

    text = path.read_text()
    data = yaml.safe_load(text)
    assert data["topology"]["nodes"]["wlc1"]["kind"] == "cisco_cat9kv"
    assert '"wlc1": "cisco_c9800cl"' in text
    assert "# dnlab-gui-node-overrides:" not in text


def test_c9800_stale_cat9kv_override_state_is_not_saved(tmp_path):
    path = tmp_path / "lab.yml"
    topo = Topology(
        name="lab",
        nodes=[
            Node(
                name="wlc1",
                kind="cisco_c9800cl",
                image="vrnetlab/cisco_c9800cl:17.12.1",
                extra={"binds": ["/tmp/old/vswitch.xml:/vswitch.xml"]},
            )
        ],
        gui_node_overrides_state={
            "wlc1": {
                "type": "cat9kv_vswitch",
                "platform": "Q200",
                "port_count": 24,
                "serial_number": "FOC12345678",
            }
        },
    )

    ContainerLabService().save_topology_to(path, topo)

    text = path.read_text()
    data = yaml.safe_load(text)
    assert "# dnlab-gui-node-overrides:" not in text
    assert "binds" not in data["topology"]["nodes"]["wlc1"]
