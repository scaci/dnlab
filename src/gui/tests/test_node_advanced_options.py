import pytest
import yaml

from app.controllers.topology_controller import TopologyController
from app.models.node import Node
from app.models.topology import Topology


def test_advanced_extra_yaml_is_written_as_clab_node_fields(tmp_path):
    path = tmp_path / "lab.yml"
    ctrl = TopologyController()
    ctrl._clab.save_topology_to(
        path,
        Topology(
            name="lab",
            nodes=[
                Node(
                    name="r1",
                    kind="linux",
                    image="alpine:latest",
                    extra={"env": {"VCPU": "2", "RAM": "2048"}, "mgmt-ipv4": "192.0.2.10"},
                )
            ],
        ),
    )

    ctrl.update_node_by_path(
        path,
        "lab",
        "r1",
        {
            "advanced_extra_yaml": """
binds:
  - /labs/r1/bootstrap.cfg:/bootstrap.cfg:ro
ports:
  - 8443:443/tcp
user: root
env:
  BOOT_DELAY: "5"
""",
        },
    )

    data = yaml.safe_load(path.read_text())
    node = data["topology"]["nodes"]["r1"]
    assert node["binds"] == ["/labs/r1/bootstrap.cfg:/bootstrap.cfg:ro"]
    assert node["ports"] == ["8443:443/tcp"]
    assert node["user"] == "root"
    assert node["mgmt-ipv4"] == "192.0.2.10"
    assert node["env"]["BOOT_DELAY"] == "5"
    assert node["env"]["VCPU"] == "2"
    assert node["env"]["RAM"] == "2048"


def test_resource_env_patch_preserves_custom_env(tmp_path):
    path = tmp_path / "lab.yml"
    ctrl = TopologyController()
    ctrl._clab.save_topology_to(
        path,
        Topology(
            name="lab",
            nodes=[
                Node(
                    name="r1",
                    kind="linux",
                    image="alpine:latest",
                    extra={"env": {"BOOT_DELAY": "5", "VCPU": "2", "RAM": "2048"}},
                )
            ],
        ),
    )

    ctrl.update_node_by_path(
        path,
        "lab",
        "r1",
        {"extra": {"env": {"VCPU": "4", "RAM": "8192"}}},
    )

    data = yaml.safe_load(path.read_text())
    node = data["topology"]["nodes"]["r1"]
    assert node["env"]["BOOT_DELAY"] == "5"
    assert node["env"]["VCPU"] == "4"
    assert node["env"]["RAM"] == "8192"


def test_advanced_extra_yaml_rejects_gui_managed_fields(tmp_path):
    path = tmp_path / "lab.yml"
    ctrl = TopologyController()
    ctrl._clab.save_topology_to(
        path,
        Topology(
            name="lab",
            nodes=[Node(name="r1", kind="linux", image="alpine:latest")],
        ),
    )

    with pytest.raises(ValueError, match="GUI-managed"):
        ctrl.update_node_by_path(
            path,
            "lab",
            "r1",
            {"advanced_extra_yaml": "image: alpine:edge"},
        )


def test_advanced_extra_yaml_requires_mapping(tmp_path):
    path = tmp_path / "lab.yml"
    ctrl = TopologyController()
    ctrl._clab.save_topology_to(
        path,
        Topology(
            name="lab",
            nodes=[Node(name="r1", kind="linux", image="alpine:latest")],
        ),
    )

    with pytest.raises(ValueError, match="must be a mapping"):
        ctrl.update_node_by_path(
            path,
            "lab",
            "r1",
            {"advanced_extra_yaml": "- not\n- a\n- mapping"},
        )
