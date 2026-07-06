"""Node-specific GUI override facade."""

from __future__ import annotations

from typing import Any

from app.models.node import Node
from app.services import node_override_plugins


def applies(kind: str | None, image: str | None = None) -> bool:
    return node_override_plugins.for_kind(kind, image) is not None


def default_state(kind: str | None, image: str | None = None) -> dict[str, Any] | None:
    plugin = node_override_plugins.for_kind(kind, image)
    return plugin.default_state(kind, image) if plugin else None


def apply_state(node: Node, state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize GUI override state through the matching plugin."""
    plugin = node_override_plugins.for_state(state, node.kind, node.image)
    if not plugin:
        for registered in node_override_plugins.all_plugins():
            registered.cleanup_node(node)
        return None
    return plugin.apply_state(node, state)


def rename_state(states: dict[str, dict[str, Any]], old: str, new: str) -> None:
    if old in states:
        states[new] = states.pop(old)
