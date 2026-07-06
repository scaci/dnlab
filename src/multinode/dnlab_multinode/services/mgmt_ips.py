"""Management network reserved address helpers."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass


class MgmtAddressError(ValueError):
    """Raised when a management subnet cannot host reserved addresses."""


@dataclass(frozen=True)
class MgmtIPv4Reservations:
    docker_gw: str
    anchor: str
    dns: str
    jumphost: str


def ipv4_reservations(subnet: str) -> MgmtIPv4Reservations:
    """Return reserved IPv4 addresses for a lab mgmt subnet.

    The first usable host is reserved for the Docker/bridge gateway. The last
    three usable hosts are reserved for the mgmt-anchor, DNS, and the
    jumphost/default gateway seen by VDs.
    """
    try:
        net = ipaddress.IPv4Network(subnet, strict=False)
    except ValueError as exc:
        raise MgmtAddressError(f"invalid IPv4 mgmt subnet {subnet!r}: {exc}") from exc

    if net.prefixlen > 29:
        raise MgmtAddressError(
            f"IPv4 mgmt subnet {net} is too small: need at least /29 "
            "for Docker gateway, mgmt anchor, DNS, jumphost, and VD addresses"
        )

    first = net.network_address + 1
    anchor = net.broadcast_address - 3
    dns = net.broadcast_address - 2
    jumphost = net.broadcast_address - 1
    if len({first, anchor, dns, jumphost}) != 4:
        raise MgmtAddressError(f"IPv4 mgmt subnet {net} is too small")

    return MgmtIPv4Reservations(
        docker_gw=str(first),
        anchor=str(anchor),
        dns=str(dns),
        jumphost=str(jumphost),
    )


def ipv6_gateway(subnet: str) -> str:
    """Return the last usable address in an IPv6 subnet."""
    try:
        net = ipaddress.IPv6Network(subnet, strict=False)
    except ValueError as exc:
        raise MgmtAddressError(f"invalid IPv6 mgmt subnet {subnet!r}: {exc}") from exc
    if net.num_addresses < 2:
        raise MgmtAddressError(f"IPv6 mgmt subnet {net} is too small")
    return str(net.network_address + (net.num_addresses - 1))


def derive_ipv6_subnet_from_ipv4(v4_subnet: str) -> str:
    """Build the default Containerlab-style IPv6 subnet from an IPv4 subnet.

    Avoid IPv4-mapped IPv6 (``::ffff:*``): Docker treats those ranges as
    overlapping their IPv4 counterparts.
    """
    try:
        net = ipaddress.IPv4Network(v4_subnet, strict=False)
    except ValueError as exc:
        raise MgmtAddressError(f"invalid IPv4 mgmt subnet {v4_subnet!r}: {exc}") from exc
    octets = str(net.network_address).split(".")
    return f"3fff:{octets[0]}:{octets[1]}:{octets[2]}::/64"
