"""Catalyst 9000V vswitch override plugin."""

from __future__ import annotations

import random
import re
import string
from pathlib import Path
from typing import Any

from app.models.node import Node


OVERRIDE_KEY = "cat9kv_vswitch"


class Cat9kvVswitchPlugin:
    key = OVERRIDE_KEY

    def applies(self, kind: str | None, image: str | None = None) -> bool:
        kind_s = (kind or "").lower()
        image_s = (image or "").lower()
        return kind_s == "cisco_cat9kv" or "cisco_cat9kv" in image_s

    def default_state(self, kind: str | None, image: str | None = None) -> dict[str, Any] | None:
        if not self.applies(kind, image):
            return None
        return {
            "type": self.key,
            "platform": "UADP",
            "port_count": 24,
            "serial_number": _random_cisco_serial(),
        }

    def apply_state(self, node: Node, state: dict[str, Any] | None) -> dict[str, Any] | None:
        """Normalize GUI override state and remove stale clab-facing binds."""
        if not self.applies(node.kind, node.image):
            self.cleanup_node(node)
            return None

        clean = _clean_state(state) or self.default_state(node.kind, node.image)
        if not clean:
            self.cleanup_node(node)
            return None

        self.cleanup_node(node)
        return clean

    def cleanup_node(self, node: Node) -> None:
        _remove_vswitch_bind(node)

    def materialize(self, node: Node, state: dict[str, Any], topology_path: Path, lab_name: str) -> None:
        clean = _clean_state(state)
        if not clean:
            self.cleanup_node(node)
            return

        asset_dir = topology_path.parent / "node-assets" / lab_name / node.name
        asset_dir.mkdir(parents=True, exist_ok=True)
        vswitch_path = asset_dir / "vswitch.xml"
        vswitch_path.write_text(_vswitch_xml(clean))

        bind = f"{vswitch_path}:/vswitch.xml"
        binds = [str(b) for b in (node.extra.get("binds") or []) if not str(b).endswith(":/vswitch.xml")]
        binds.append(bind)
        node.extra["binds"] = binds


def _clean_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    platform = str(state.get("platform") or "UADP").upper()
    if platform not in {"UADP", "Q200"}:
        platform = "UADP"
    try:
        port_count = int(state.get("port_count") or 24)
    except (TypeError, ValueError):
        port_count = 24
    port_count = max(1, min(port_count, 256))
    serial = _clean_serial(state.get("serial_number")) or _random_cisco_serial()
    return {
        "type": OVERRIDE_KEY,
        "platform": platform,
        "port_count": port_count,
        "serial_number": serial,
    }


def _clean_serial(value: Any) -> str | None:
    serial = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if not serial:
        return None
    return serial[:12]


def _random_cisco_serial() -> str:
    prefix = random.choice(["FOC", "FDO", "FXS", "CAT"])
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"{prefix}{suffix}"


def _vswitch_xml(state: dict[str, Any]) -> str:
    serial = state["serial_number"]
    return (
        "<switch>\n"
        f"  <asic_type>{state['platform']}</asic_type>\n"
        f"  <port_count>{state['port_count']}</port_count>\n"
        f"  <serial_number>{serial}</serial_number>\n"
        f"  <prod_serial_number>{serial}</prod_serial_number>\n"
        "</switch>\n"
    )


def _remove_vswitch_bind(node: Node) -> None:
    binds = [str(b) for b in (node.extra.get("binds") or []) if not str(b).endswith(":/vswitch.xml")]
    if binds:
        node.extra["binds"] = binds
    else:
        node.extra.pop("binds", None)


PLUGIN = Cat9kvVswitchPlugin()
