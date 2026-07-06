"""Deterministic ID generation for VxLAN, VRF, etc."""

import binascii


def _hash(lab_name: str) -> int:
    return binascii.crc32(lab_name.encode()) & 0xFFFFFFFF


def vrf_table_id(lab_name: str) -> int:
    """VRF table ID in range 100-999."""
    return 100 + (_hash(lab_name) % 900)


def mgmt_vxlan_id(lab_name: str) -> int:
    """Management VxLAN ID in range 2000-2999."""
    return 2000 + (_hash(lab_name) % 1000)


def dataplane_vxlan_base(lab_name: str) -> int:
    """Base VxLAN ID for dataplane tunnels in range 3000-3999."""
    return 3000 + (_hash(lab_name) % 1000)


def runtime_relay_port(lab_name: str) -> int:
    """TCP port exposed on each runtime host for the per-lab runtime relay.

    Range 23000-23999. A relay with the same lab uses the same port on every
    host; different hosts have independent port namespaces.
    """
    return 23000 + (_hash(lab_name) % 1000)


def realnet_vxlan_id(lab_name: str, index: int = 0) -> int:
    """Base VNI for real_net bridge fabrics. One VNI per GUI real_net."""
    return 5000 + ((_hash(lab_name) + index) % 1000)
