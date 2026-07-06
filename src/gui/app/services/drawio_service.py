"""
draw.io / mxGraph XML import-export service.

draw.io XML format uses mxCell elements:
  - vertex="1"  → network node
  - edge="1"    → link between nodes
"""

import json
import math
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Any

from app.models.node import Node, NodePosition
from app.models.link import Link
from app.models.topology import Topology
from app.services.drawio_icons import NODE_ICON_SIZE, node_icon_data_uri

PARALLEL_EDGE_STEP_PX = 40.0

# Map draw.io style keywords → clab kind (kind names match containerlab 0.74)
_STYLE_KIND_MAP: dict[str, str] = {
    "cisco.routers.router":      "cisco_xrv9k",
    "cisco.routers.cisco_router":"cisco_xrv9k",
    "cisco.switches.layer_3":    "cisco_n9kv",
    "cisco.switches.multilayer": "cisco_n9kv",
    "cisco.firewalls":           "cisco_vios",
    "juniper.router":            "juniper_vmx",
    "juniper.ex_switch":         "juniper_vjunosswitch",
    "arista":                    "arista_ceos",
    "nokia":                     "nokia_srlinux",
    "linux":                     "linux",
    "server":                    "linux",
    "router":                    "cisco_xrv9k",
    "switch":                    "cisco_n9kv",
    "firewall":                  "cisco_vios",
}


class DrawioService:
    """Convert between Topology models and draw.io XML."""

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def from_xml(self, xml_str: str, topology_name: str = "imported") -> Topology:
        """Parse draw.io XML and return a Topology."""
        root = ET.fromstring(xml_str)
        # Support both root <mxGraphModel> and wrapped <mxfile><diagram>...
        graph_model = root if root.tag == "mxGraphModel" else root.find(".//mxGraphModel")
        if graph_model is None:
            raise ValueError("No mxGraphModel element found in XML")

        cells = {
            cell.get("id"): cell
            for cell in graph_model.iter("mxCell")
        }

        nodes: list[Node] = []
        links: list[Link] = []
        extra: dict[str, Any] = {}
        meta = cells.get("dnlab_meta")
        if meta is not None:
            mgmt = self._json_attr_or(meta.get("dnlab_mgmt"), {})
            if isinstance(mgmt, dict) and mgmt:
                extra["mgmt"] = mgmt

        # First pass: vertices (nodes)
        vertex_ids: dict[str, str] = {}  # mxCell id → node name
        for cell_id, cell in cells.items():
            if cell.get("vertex") == "1" and cell.get("parent") not in ("", None, "0"):
                label = cell.get("value") or cell_id
                node_name = self._sanitize_name(label)
                style = cell.get("style", "")
                kind = cell.get("dnlab_kind") or self._style_to_kind(style)
                image = cell.get("dnlab_image") or ""
                extra_data = self._json_attr_or(cell.get("dnlab_extra"), {})
                if not isinstance(extra_data, dict):
                    extra_data = {}

                geo = cell.find("mxGeometry")
                x = float(geo.get("x", 100)) if geo is not None else 100.0
                y = float(geo.get("y", 100)) if geo is not None else 100.0

                nodes.append(
                    Node(
                        name=node_name,
                        kind=kind,
                        image=image,
                        position=NodePosition(x=x, y=y),
                        extra=extra_data,
                    )
                )
                vertex_ids[cell_id] = node_name

        # Second pass: edges (links)
        for cell in cells.values():
            if cell.get("edge") == "1":
                src_id = cell.get("source", "")
                tgt_id = cell.get("target", "")
                if src_id in vertex_ids and tgt_id in vertex_ids:
                    src_iface = cell.get("dnlab_source_iface")
                    tgt_iface = cell.get("dnlab_target_iface")
                    if src_iface is None and tgt_iface is None:
                        label = cell.get("value") or ""
                        src_iface, tgt_iface = self._parse_link_label(label)
                    links.append(
                        Link(
                            source=vertex_ids[src_id],
                            source_iface=src_iface or "",
                            target=vertex_ids[tgt_id],
                            target_iface=tgt_iface or "",
                        )
                    )

        return Topology(name=topology_name, nodes=nodes, links=links, extra=extra)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def to_xml(self, topology: Topology) -> str:
        """Serialize a Topology to draw.io XML."""
        graph_model = ET.Element(
            "mxGraphModel",
            dx="1422", dy="762", grid="1", gridSize="10",
            guides="1", tooltips="1", connect="1", arrows="1",
            fold="1", page="1", pageScale="1",
            pageWidth="1169", pageHeight="827",
            math="0", shadow="0",
        )
        root_el = ET.SubElement(graph_model, "root")
        ET.SubElement(root_el, "mxCell", id="0")
        ET.SubElement(root_el, "mxCell", id="1", parent="0")
        self._append_dnlab_metadata(root_el, topology)

        node_ids: dict[str, str] = {}
        cell_id = 2

        for node in topology.nodes:
            cid = str(cell_id)
            node_ids[node.name] = cid
            cell_id += 1

            style = self._node_style(node.kind)

            attrs = {
                "id": cid,
                "value": node.name,
                "style": style,
                "vertex": "1",
                "parent": "1",
                "dnlab_kind": node.kind,
                "dnlab_image": node.image or "",
                "dnlab_extra": self._json_attr(node.extra or {}),
            }
            mgmt_ipv4 = node.mgmt_ipv4 or (node.extra or {}).get("mgmt-ipv4") or ""
            mgmt_ipv6 = node.mgmt_ipv6 or (node.extra or {}).get("mgmt-ipv6") or ""
            if mgmt_ipv4:
                attrs["dnlab_mgmt_ipv4"] = str(mgmt_ipv4)
            if mgmt_ipv6:
                attrs["dnlab_mgmt_ipv6"] = str(mgmt_ipv6)

            cell = ET.SubElement(root_el, "mxCell", **attrs)
            ET.SubElement(
                cell, "mxGeometry",
                x=str(node.position.x), y=str(node.position.y),
                width=str(NODE_ICON_SIZE), height=str(NODE_ICON_SIZE), **{"as": "geometry"},
            )

        parallel_groups = self._parallel_link_groups(topology.links)

        for link in topology.links:
            src_id = node_ids.get(link.source)
            tgt_id = node_ids.get(link.target)
            if not src_id or not tgt_id:
                continue

            source_node = topology.get_node(link.source)
            target_node = topology.get_node(link.target)
            label = self._link_label(link, source_node, target_node)
            link_type = self._link_type(source_node, target_node)

            cid = str(cell_id)
            cell_id += 1

            cell = ET.SubElement(
                root_el, "mxCell",
                id=cid, value=label,
                style=self._edge_style(),
                edge="1", source=src_id, target=tgt_id, parent="1",
                dnlab_source_iface=link.source_iface or "",
                dnlab_target_iface=link.target_iface or "",
                dnlab_link_type=link_type,
            )
            geometry = ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})
            waypoint = self._parallel_link_waypoint(link, topology, parallel_groups)
            if waypoint is not None:
                points = ET.SubElement(geometry, "Array", **{"as": "points"})
                ET.SubElement(
                    points,
                    "mxPoint",
                    x=self._fmt_float(waypoint[0]),
                    y=self._fmt_float(waypoint[1]),
                )

        return ET.tostring(graph_model, encoding="unicode", xml_declaration=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_name(label: str) -> str:
        """Convert a label to a valid ContainerLab node name."""
        import re
        name = re.sub(r"[^a-zA-Z0-9_-]", "_", label.strip())
        return name or "node"

    @staticmethod
    def _style_to_kind(style: str) -> str:
        style_lower = style.lower()
        for fragment, kind in _STYLE_KIND_MAP.items():
            if fragment.lower() in style_lower:
                return kind
        return "linux"

    @staticmethod
    def _parse_link_label(label: str) -> tuple[str, str]:
        """Extract interface names from an edge label like 'eth1 – eth2'."""
        separators = [" – ", " - ", "/", ":"]
        for sep in separators:
            if sep in label:
                parts = label.split(sep, 1)
                return parts[0].strip(), parts[1].strip()
        return "", ""

    @staticmethod
    def _json_attr(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _json_attr_or(value: str | None, fallback: Any) -> Any:
        if not value:
            return fallback
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return fallback

    def _append_dnlab_metadata(self, root_el: ET.Element, topology: Topology) -> None:
        attrs = {
            "id": "dnlab_meta",
            "parent": "0",
            "dnlab_version": "1",
            "dnlab_topology_name": topology.name,
        }
        mgmt = (topology.extra or {}).get("mgmt")
        if mgmt:
            attrs["dnlab_mgmt"] = self._json_attr(mgmt)
        ET.SubElement(root_el, "mxCell", **attrs)

    @staticmethod
    def _node_style(kind: str | None) -> str:
        image = node_icon_data_uri(kind)
        return (
            "shape=image;"
            "html=1;"
            "imageAspect=0;"
            "aspect=fixed;"
            f"image={image};"
            "verticalLabelPosition=bottom;"
            "verticalAlign=top;"
            "align=center;"
        )

    @staticmethod
    def _edge_style() -> str:
        return (
            "html=1;"
            "rounded=0;"
            "curved=1;"
            "startArrow=none;"
            "endArrow=none;"
            "sourceArrow=none;"
            "targetArrow=none;"
        )

    @staticmethod
    def _link_type(source_node: Node | None, target_node: Node | None) -> str:
        if (
            (source_node and source_node.kind == "_real_net")
            or (target_node and target_node.kind == "_real_net")
        ):
            return "real_net"
        return "data"

    @classmethod
    def _link_label(cls, link: Link, source_node: Node | None, target_node: Node | None) -> str:
        if source_node and source_node.kind == "_real_net":
            return link.target_iface or ""
        if target_node and target_node.kind == "_real_net":
            return link.source_iface or ""
        label_parts = []
        if link.source_iface:
            label_parts.append(link.source_iface)
        if link.target_iface:
            label_parts.append(link.target_iface)
        return " – ".join(label_parts)

    @classmethod
    def _parallel_link_groups(cls, links: list[Link]) -> dict[tuple[str, str], list[Link]]:
        groups: dict[tuple[str, str], list[Link]] = defaultdict(list)
        for link in links:
            groups[cls._link_pair_key(link)].append(link)
        return {
            key: sorted(group, key=cls._link_sort_key)
            for key, group in groups.items()
            if len(group) > 1
        }

    @staticmethod
    def _link_pair_key(link: Link) -> tuple[str, str]:
        return tuple(sorted((link.source, link.target)))

    @staticmethod
    def _link_sort_key(link: Link) -> tuple[str, str, str, str]:
        return (link.source, link.target, link.source_iface or "", link.target_iface or "")

    @classmethod
    def _parallel_link_waypoint(
        cls,
        link: Link,
        topology: Topology,
        parallel_groups: dict[tuple[str, str], list[Link]],
    ) -> tuple[float, float] | None:
        siblings = parallel_groups.get(cls._link_pair_key(link))
        if not siblings:
            return None
        idx = next((i for i, sibling in enumerate(siblings) if sibling is link), -1)
        if idx < 0:
            return None

        pair = cls._link_pair_key(link)
        source = topology.get_node(pair[0])
        target = topology.get_node(pair[1])
        if not source or not target:
            return None

        offset = (idx - ((len(siblings) - 1) / 2)) * PARALLEL_EDGE_STEP_PX
        x0, y0 = source.position.x, source.position.y
        x1, y1 = target.position.x, target.position.y
        dx = x1 - x0
        dy = y1 - y0
        length = math.hypot(dx, dy)
        if length <= 0:
            nx, ny = 0.0, 1.0
        else:
            nx, ny = -dy / length, dx / length

        return ((x0 + x1) / 2 + nx * offset, (y0 + y1) / 2 + ny * offset)

    @staticmethod
    def _fmt_float(value: float) -> str:
        return f"{value:.1f}".rstrip("0").rstrip(".")
