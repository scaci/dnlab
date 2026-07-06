"""draw.io icon rendering helpers backed by the GUI device catalog."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET

from app.config import settings
from app.services import device_catalog


NODE_ICON_SIZE = 56
NODE_GLYPH_SIZE = 48
NODE_GLYPH_OFFSET = (NODE_ICON_SIZE - NODE_GLYPH_SIZE) / 2


def node_icon_data_uri(kind: str | None) -> str:
    """Return a draw.io-safe SVG data URI matching the canvas node icon."""
    color = (
        device_catalog.vendor_color("generic")
        if kind == "_real_net"
        else device_catalog.vendor_color(device_catalog.kind_vendor(kind))
    )
    icon_ref = (
        device_catalog.icon_path("cloud")
        if kind == "_real_net"
        else device_catalog.icon_path(device_catalog.kind_type(kind))
    )
    raw_svg = _read_static_svg(icon_ref)
    svg = _compose_node_icon(raw_svg, color) if raw_svg else _fallback_node_icon(color)
    return "data:image/svg+xml," + quote(svg, safe="")


def _read_static_svg(icon_ref: str) -> str:
    if not icon_ref:
        return ""
    path = Path(icon_ref)
    if not path.is_absolute():
        path = settings.STATIC_DIR / icon_ref
    try:
        resolved = path.resolve()
        static_root = settings.STATIC_DIR.resolve()
        if static_root not in resolved.parents and resolved != static_root:
            return ""
        return resolved.read_text(encoding="utf-8")
    except OSError:
        return ""


def _compose_node_icon(raw_svg: str, color: str) -> str:
    try:
        source_svg = ET.fromstring(raw_svg)
    except ET.ParseError:
        return _fallback_node_icon(color)

    view_box = _parse_view_box(source_svg.get("viewBox"))
    scale = NODE_GLYPH_SIZE / max(view_box["width"], view_box["height"])
    x = NODE_GLYPH_OFFSET + ((NODE_GLYPH_SIZE - view_box["width"] * scale) / 2)
    y = NODE_GLYPH_OFFSET + ((NODE_GLYPH_SIZE - view_box["height"] * scale) / 2)
    inner = "".join(ET.tostring(child, encoding="unicode") for child in list(source_svg))

    return f"""<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE svg>
<svg xmlns="http://www.w3.org/2000/svg" width="{NODE_ICON_SIZE}" height="{NODE_ICON_SIZE}" viewBox="0 0 {NODE_ICON_SIZE} {NODE_ICON_SIZE}">
  <circle cx="28" cy="28" r="27" fill="{_xml_attr(color)}"/>
  <g transform="translate({_fmt_float(x)} {_fmt_float(y)}) scale({_fmt_float(scale)}) translate({_fmt_float(-view_box['x'])} {_fmt_float(-view_box['y'])})"
     fill="none"
     stroke="#ffffff"
     stroke-width="3"
     stroke-linecap="round"
     stroke-linejoin="round">
    {inner}
  </g>
</svg>"""


def _fallback_node_icon(color: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE svg>
<svg xmlns="http://www.w3.org/2000/svg" width="{NODE_ICON_SIZE}" height="{NODE_ICON_SIZE}" viewBox="0 0 {NODE_ICON_SIZE} {NODE_ICON_SIZE}">
  <circle cx="28" cy="28" r="27" fill="{_xml_attr(color)}"/>
</svg>"""


def _parse_view_box(view_box: str | None) -> dict[str, float]:
    try:
        values = [float(v) for v in (view_box or "0 0 64 64").replace(",", " ").split()]
    except ValueError:
        values = []
    if len(values) != 4 or values[2] <= 0 or values[3] <= 0:
        values = [0.0, 0.0, 64.0, 64.0]
    return {"x": values[0], "y": values[1], "width": values[2], "height": values[3]}


def _fmt_float(value: float) -> str:
    return f"{value:.6g}"


def _xml_attr(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
