"""Tests for deterministic ID generation."""

from dnlab_multinode.utils import ids


def test_vrf_table_range():
    for name in ["lab1", "triangle", "big-lab-name", "x"]:
        val = ids.vrf_table_id(name)
        assert 100 <= val <= 999


def test_mgmt_vxlan_range():
    for name in ["lab1", "triangle", "big-lab-name", "x"]:
        val = ids.mgmt_vxlan_id(name)
        assert 2000 <= val <= 2999


def test_dataplane_vxlan_range():
    for name in ["lab1", "triangle", "big-lab-name", "x"]:
        val = ids.dataplane_vxlan_base(name)
        assert 3000 <= val <= 3999


def test_runtime_relay_port_range():
    for name in ["lab1", "triangle", "big-lab-name", "x"]:
        val = ids.runtime_relay_port(name)
        assert 23000 <= val <= 23999


def test_vxlan_id_determinism():
    """Same lab name → same IDs (stable across runs)."""
    for name in ["triangle", "acme-prod", "lab42"]:
        assert ids.vrf_table_id(name) == ids.vrf_table_id(name)
        assert ids.mgmt_vxlan_id(name) == ids.mgmt_vxlan_id(name)
        assert ids.dataplane_vxlan_base(name) == ids.dataplane_vxlan_base(name)


def test_different_names_different_ids():
    """Different lab names should (almost always) give different IDs."""
    a = ids.mgmt_vxlan_id("alpha")
    b = ids.mgmt_vxlan_id("bravo")
    # Not a strict requirement of the hash, but should hold for these names
    assert a != b
