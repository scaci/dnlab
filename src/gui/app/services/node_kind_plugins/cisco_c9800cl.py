"""C9800-CL load-time compatibility migrations."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.models.topology import Topology


log = logging.getLogger(__name__)


class CiscoC9800ClPlugin:
    kind = "cisco_c9800cl"

    def migrate_topology(self, topo: Topology, path: Path) -> None:
        """Repair C9800 links saved while the catalog marked G2 as mgmt.

        The old GUI hid ``eth1`` because ``GigabitEthernet2`` matched the first
        data-port display name. Those topologies therefore start at ``eth2`` and
        make vrnetlab insert a placeholder NIC. C9800 management is IOS
        ``GigabitEthernet2``; data links must stay contiguous from containerlab
        ``eth1`` (IOS ``GigabitEthernet3``).
        """
        c9800_nodes = {
            node.name
            for node in topo.nodes
            if (node.kind or "").lower() == self.kind
        }
        if not c9800_nodes:
            return

        migrated = 0
        for node_name in c9800_nodes:
            used: list[int] = []
            for link in topo.links:
                for endpoint, iface in (
                    (link.source, link.source_iface),
                    (link.target, link.target_iface),
                ):
                    if endpoint != node_name:
                        continue
                    match = re.fullmatch(r"eth([1-9]\d*)", iface or "")
                    if match:
                        used.append(int(match.group(1)))
            if not used or min(used) != 2 or max(used) > 4:
                continue

            for link in topo.links:
                if link.source == node_name:
                    link.source_iface = _shift_old_data_iface(link.source_iface)
                if link.target == node_name:
                    link.target_iface = _shift_old_data_iface(link.target_iface)
            migrated += 1

        if migrated:
            log.info(
                "Topology %s: migrated %d C9800 node(s) from old G2-mgmt "
                "sparse data ports to contiguous data ports",
                path,
                migrated,
            )


def _shift_old_data_iface(iface: str) -> str:
    match = re.fullmatch(r"eth([2-4])", iface or "")
    if not match:
        return iface
    return f"eth{int(match.group(1)) - 1}"


PLUGIN = CiscoC9800ClPlugin()

