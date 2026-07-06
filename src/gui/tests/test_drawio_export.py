import json
import xml.etree.ElementTree as ET

from app.models.link import Link
from app.models.node import Node, NodePosition
from app.models.topology import Topology
from app.services.drawio_service import DrawioService


def _cells(xml: str) -> list[ET.Element]:
    root = ET.fromstring(xml)
    return list(root.iter("mxCell"))


def _cell_by_value(xml: str, value: str) -> ET.Element:
    for cell in _cells(xml):
        if cell.get("value") == value:
            return cell
    raise AssertionError(f"cell with value {value!r} not found")


def _edges(xml: str) -> list[ET.Element]:
    return [cell for cell in _cells(xml) if cell.get("edge") == "1"]


def _edge_point(edge: ET.Element) -> tuple[float, float] | None:
    point = edge.find("./mxGeometry/Array/mxPoint")
    if point is None:
        return None
    return float(point.get("x")), float(point.get("y"))


def _assert_network_edge_style(edge: ET.Element) -> None:
    style = edge.get("style") or ""
    assert "orthogonalEdgeStyle" not in style
    assert "curved=1;" in style
    assert "startArrow=none;" in style
    assert "endArrow=none;" in style
    assert "sourceArrow=none;" in style
    assert "targetArrow=none;" in style


def test_export_node_uses_canvas_image_icon_and_dnlab_metadata():
    topo = Topology(
        name="lab",
        nodes=[
            Node(
                name="r1",
                kind="cisco_xrv9k",
                image="vrnetlab/cisco_xrv9k:latest",
                position=NodePosition(x=12, y=34),
                extra={"mgmt-ipv4": "172.20.20.10"},
            )
        ],
    )

    xml = DrawioService().to_xml(topo)
    cell = _cell_by_value(xml, "r1")
    style = cell.get("style") or ""

    assert "shape=image;" in style
    assert "image=data:image/svg+xml," in style
    assert "%23049fd9" in style
    assert cell.get("dnlab_kind") == "cisco_xrv9k"
    assert cell.get("dnlab_image") == "vrnetlab/cisco_xrv9k:latest"
    assert json.loads(cell.get("dnlab_extra") or "{}") == {"mgmt-ipv4": "172.20.20.10"}
    assert cell.get("dnlab_mgmt_ipv4") == "172.20.20.10"
    geo = cell.find("mxGeometry")
    assert geo is not None
    assert geo.get("width") == "56"
    assert geo.get("height") == "56"


def test_export_unknown_kind_uses_image_fallback_without_crashing():
    topo = Topology(
        name="lab",
        nodes=[Node(name="x1", kind="unknown_kind", image="example/unknown:latest")],
    )

    xml = DrawioService().to_xml(topo)
    cell = _cell_by_value(xml, "x1")

    assert "shape=image;" in (cell.get("style") or "")
    assert cell.get("dnlab_kind") == "unknown_kind"


def test_export_catalog_kind_needs_no_drawio_service_hardcoded_style():
    topo = Topology(
        name="lab",
        nodes=[Node(name="ow1", kind="openwrt", image="openwrt:latest")],
    )

    xml = DrawioService().to_xml(topo)
    cell = _cell_by_value(xml, "ow1")

    assert "shape=image;" in (cell.get("style") or "")
    assert cell.get("dnlab_kind") == "openwrt"


def test_export_realnet_uses_cloud_image_and_realnet_link_label():
    topo = Topology(
        name="lab",
        nodes=[
            Node(name="wan", kind="_real_net", image=""),
            Node(name="r1", kind="linux", image="alpine:latest"),
        ],
        links=[Link(source="wan", source_iface="real", target="r1", target_iface="eth1")],
    )

    xml = DrawioService().to_xml(topo)
    realnet = _cell_by_value(xml, "wan")
    edge = next(cell for cell in _edges(xml))

    assert "shape=image;" in (realnet.get("style") or "")
    assert realnet.get("dnlab_kind") == "_real_net"
    assert edge.get("value") == "eth1"
    assert edge.get("dnlab_link_type") == "real_net"
    assert _edge_point(edge) is None
    _assert_network_edge_style(edge)


def test_export_data_link_label_and_endpoint_metadata():
    topo = Topology(
        name="lab",
        nodes=[
            Node(name="r1", kind="linux", image="alpine:latest"),
            Node(name="r2", kind="linux", image="alpine:latest"),
        ],
        links=[Link(source="r1", source_iface="eth1", target="r2", target_iface="eth2")],
    )

    xml = DrawioService().to_xml(topo)
    edge = next(cell for cell in _edges(xml))

    assert edge.get("value") == "eth1 – eth2"
    assert edge.get("dnlab_source_iface") == "eth1"
    assert edge.get("dnlab_target_iface") == "eth2"
    assert edge.get("dnlab_link_type") == "data"
    assert _edge_point(edge) is None
    _assert_network_edge_style(edge)


def test_export_mgmt_is_metadata_not_graphical_cells():
    topo = Topology(
        name="lab",
        nodes=[Node(name="r1", kind="linux", image="alpine:latest")],
        extra={
            "mgmt": {
                "ipv4-subnet": "172.20.20.0/24",
                "ipv4-gw": "172.20.20.254",
                "canvas_pos": {"x": 80, "y": 80},
            }
        },
    )

    xml = DrawioService().to_xml(topo)
    cells = _cells(xml)
    meta = next(cell for cell in cells if cell.get("id") == "dnlab_meta")
    vertices = [cell for cell in cells if cell.get("vertex") == "1"]
    edges = [cell for cell in cells if cell.get("edge") == "1"]

    assert json.loads(meta.get("dnlab_mgmt") or "{}") == topo.extra["mgmt"]
    assert len(vertices) == 1
    assert vertices[0].get("value") == "r1"
    assert edges == []


def test_export_parallel_links_get_distinct_waypoints():
    topo = Topology(
        name="lab",
        nodes=[
            Node(name="n9kv1", kind="cisco_n9kv", image="", position=NodePosition(x=100, y=100)),
            Node(name="n9kv2", kind="cisco_n9kv", image="", position=NodePosition(x=300, y=100)),
        ],
        links=[
            Link(source="n9kv1", source_iface="eth1", target="n9kv2", target_iface="eth1"),
            Link(source="n9kv1", source_iface="eth2", target="n9kv2", target_iface="eth2"),
        ],
    )

    xml = DrawioService().to_xml(topo)
    points = [_edge_point(edge) for edge in _edges(xml)]

    assert len(points) == 2
    assert points == [(200.0, 80.0), (200.0, 120.0)]
    for edge in _edges(xml):
        _assert_network_edge_style(edge)


def test_export_three_parallel_links_have_symmetric_deterministic_offsets():
    topo = Topology(
        name="lab",
        nodes=[
            Node(name="a", kind="linux", image="", position=NodePosition(x=0, y=0)),
            Node(name="b", kind="linux", image="", position=NodePosition(x=100, y=0)),
        ],
        links=[
            Link(source="a", source_iface="eth2", target="b", target_iface="eth2"),
            Link(source="a", source_iface="eth1", target="b", target_iface="eth1"),
            Link(source="a", source_iface="eth3", target="b", target_iface="eth3"),
        ],
    )

    xml = DrawioService().to_xml(topo)
    by_label = {edge.get("value"): _edge_point(edge) for edge in _edges(xml)}

    assert by_label == {
        "eth2 – eth2": (50.0, 0.0),
        "eth1 – eth1": (50.0, -40.0),
        "eth3 – eth3": (50.0, 40.0),
    }


def test_export_reverse_direction_parallel_links_share_group():
    topo = Topology(
        name="lab",
        nodes=[
            Node(name="a", kind="linux", image="", position=NodePosition(x=0, y=0)),
            Node(name="b", kind="linux", image="", position=NodePosition(x=100, y=0)),
        ],
        links=[
            Link(source="a", source_iface="eth1", target="b", target_iface="eth1"),
            Link(source="b", source_iface="eth2", target="a", target_iface="eth2"),
        ],
    )

    xml = DrawioService().to_xml(topo)
    points = [_edge_point(edge) for edge in _edges(xml)]

    assert len(points) == 2
    assert len(set(points)) == 2


def test_export_parallel_realnet_links_keep_labels_and_get_waypoints():
    topo = Topology(
        name="lab",
        nodes=[
            Node(name="net1", kind="_real_net", image="", position=NodePosition(x=0, y=0)),
            Node(name="r1", kind="linux", image="", position=NodePosition(x=100, y=0)),
        ],
        links=[
            Link(source="net1", source_iface="real", target="r1", target_iface="eth1"),
            Link(source="net1", source_iface="real", target="r1", target_iface="eth2"),
        ],
    )

    xml = DrawioService().to_xml(topo)
    edges = _edges(xml)

    assert [edge.get("value") for edge in edges] == ["eth1", "eth2"]
    assert [edge.get("dnlab_link_type") for edge in edges] == ["real_net", "real_net"]
    assert len({_edge_point(edge) for edge in edges}) == 2


def test_export_parallel_links_with_same_position_get_fallback_waypoints():
    topo = Topology(
        name="lab",
        nodes=[
            Node(name="a", kind="linux", image="", position=NodePosition(x=42, y=42)),
            Node(name="b", kind="linux", image="", position=NodePosition(x=42, y=42)),
        ],
        links=[
            Link(source="a", source_iface="eth1", target="b", target_iface="eth1"),
            Link(source="a", source_iface="eth2", target="b", target_iface="eth2"),
        ],
    )

    xml = DrawioService().to_xml(topo)
    points = [_edge_point(edge) for edge in _edges(xml)]

    assert points == [(42.0, 22.0), (42.0, 62.0)]
