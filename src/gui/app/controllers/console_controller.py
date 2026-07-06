"""Console controller: maps topology node → runtime relay → PTY session.

The kind (``cisco_n9kv``, ``juniper_vjunosswitch``, …) is read from the
topology file rather than from a live inspect, because in multinode the
containers can live on workers and ``containerlab inspect`` on the master
wouldn't see them. The GUI dispatch is relay-driven: the backend resolves the
host-local runtime relay from deployment state and lets it open the
container-side serial console.

All identifiers that reach the wire come from :attr:`ResolvedLab.netname` —
unique for lab regardless of display name — so two users with "demo" labs
can't accidentally console into each other's devices.
"""

import logging

from fastapi import WebSocket

from app.models.lab import ContainerInfo
from app.services.console_service import ConsoleService
from app.services.containerlab_service import ContainerLabService
from app.services.lab_resolver import ResolvedLab
from app.services.multinode_service import multinode

log = logging.getLogger(__name__)


class ConsoleController:
    def __init__(self) -> None:
        self._clab = ContainerLabService()
        self._console = ConsoleService()

    async def open_console(
        self,
        websocket: WebSocket,
        lab: ResolvedLab,
        node_name: str,
    ) -> None:
        kind = self._lookup_kind(lab, node_name)
        container_name, mgmt_ipv4 = await self._resolve_runtime_info(lab, node_name)
        container = ContainerInfo(
            name=container_name,
            kind=kind,
            node_name=node_name,
            lab_name=lab.netname,
            ipv4_address=mgmt_ipv4,
        )

        relay = await multinode.resolve_runtime_relay(lab, node_name)
        host_hint = f"relay {relay['relay_host']}"
        log.info("open_console: %s/%s (display=%s) kind=%s host=%s",
                 lab.netname, node_name, lab.display_name,
                 kind or "<unknown>", host_hint)
        await websocket.send_text(
            f"\r\n[Connecting to {container.name} on {host_hint}...]\r\n"
        )
        await self._console.attach_relay(websocket, relay)

    async def _resolve_runtime_info(self, lab: ResolvedLab, node_name: str) -> tuple[str, str]:
        """Return live per-VD container name and mgmt IPv4 for a topology node."""
        try:
            status = await multinode.status(lab, emit_events=False)
            node = (status.get("nodes") or {}).get(node_name) or {}
            container = node.get("container")
            mgmt_ipv4 = node.get("mgmt_ipv4") or ""
            if container or mgmt_ipv4:
                return container or node_name, mgmt_ipv4
        except Exception as exc:
            log.warning(
                "open_console: cannot resolve live container for %s/%s: %s",
                lab.netname, node_name, exc,
            )
        return node_name, ""

    def _lookup_kind(self, lab: ResolvedLab, node_name: str) -> str:
        if not lab.yaml_path.exists():
            return ""
        try:
            topo = self._clab.load_topology_from_file(lab.yaml_path)
        except Exception as exc:
            log.warning("open_console: cannot read %s: %s", lab.yaml_path, exc)
            return ""
        node = next((n for n in topo.nodes if n.name == node_name), None)
        return node.kind if node else ""
