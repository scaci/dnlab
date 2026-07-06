"""Scheduling plan model."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VDResources:
    """Resource requirements for a single VD."""
    name: str
    image: str
    cpu: int
    ram_mb: int
    cpu_source: str = ""
    ram_mb_source: str = ""

    @property
    def weight(self) -> int:
        return self.cpu * self.ram_mb


@dataclass
class HostResources:
    """Available resources on a host."""
    name: str
    host: str
    cpu_available: int
    ram_mb_available: int
    is_master: bool = False


@dataclass
class HostAssignment:
    """VDs assigned to a specific host."""
    host_name: str
    host_ip: str
    vd_names: list[str] = field(default_factory=list)
    cpu_used: int = 0
    ram_mb_used: int = 0


@dataclass
class CrossHostLink:
    """A link that crosses host boundaries and needs VxLAN."""
    vxlan_id: int
    source_node: str
    source_iface: str
    target_node: str
    target_iface: str
    source_host: str     # host name where source lives
    target_host: str     # host name where target lives
    source_host_iface: str = ""   # host-side interface name
    target_host_iface: str = ""

    def __str__(self) -> str:
        return f"{self.source_node}:{self.source_iface} <-> {self.target_node}:{self.target_iface}"


@dataclass
class SchedulePlan:
    """Complete scheduling plan."""
    lab_name: str
    assignments: dict[str, HostAssignment]   # host_name → assignment
    cross_host_links: list[CrossHostLink] = field(default_factory=list)
    local_links: list = field(default_factory=list)   # links within same host
    vrf_table_id: int = 0
    mgmt_vxlan_id: int = 0

    def host_for_vd(self, vd_name: str) -> str | None:
        """Return host name where a VD is assigned."""
        for host_name, assignment in self.assignments.items():
            if vd_name in assignment.vd_names:
                return host_name
        return None
