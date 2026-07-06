"""Registry for per-kind topology load migrations."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.models.topology import Topology


class NodeKindPlugin(Protocol):
    kind: str

    def migrate_topology(self, topo: Topology, path: Path) -> None:
        ...


def _plugins() -> list[NodeKindPlugin]:
    from app.services.node_kind_plugins import cisco_c9800cl

    return [
        cisco_c9800cl.PLUGIN,
    ]


def migrate_topology(topo: Topology, path: Path) -> None:
    for plugin in _plugins():
        plugin.migrate_topology(topo, path)

