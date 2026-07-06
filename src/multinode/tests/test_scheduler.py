"""Tests for the FFD scheduler."""

import pytest

from dnlab_multinode.models.topology import VDNode, Link
from dnlab_multinode.services.scheduler import compute_schedule, ScheduleError


def test_master_gets_lightest(topo_factory, vd_factory, host_factory):
    """Heavy VDs should go to workers; light VD may land on master."""
    nodes = {
        "heavy1": VDNode(name="heavy1", kind="linux", image="x"),
        "heavy2": VDNode(name="heavy2", kind="linux", image="x"),
        "light":  VDNode(name="light",  kind="linux", image="x"),
    }
    topo = topo_factory(nodes=nodes, links=[], num_workers=2)

    vds = vd_factory({
        "heavy1": (8, 16384),
        "heavy2": (8, 16384),
        "light":  (1, 1024),
    })
    hosts = host_factory({
        "master":  (16, 32768),
        "worker1": (16, 32768),
        "worker2": (16, 32768),
    })

    plan = compute_schedule(topo, vds, hosts)

    # Heavy VDs should not be on master (their weight > avg)
    assert "heavy1" not in plan.assignments["master"].vd_names
    assert "heavy2" not in plan.assignments["master"].vd_names


def test_ffd_balance(topo_factory, vd_factory, host_factory):
    """FFD should spread VDs across hosts, not stack them on one."""
    nodes = {f"R{i}": VDNode(name=f"R{i}", kind="linux", image="x") for i in range(1, 5)}
    topo = topo_factory(nodes=nodes, links=[], num_workers=2)

    vds = vd_factory({f"R{i}": (4, 4096) for i in range(1, 5)})
    hosts = host_factory({
        "master":  (8, 8192),
        "worker1": (8, 8192),
        "worker2": (8, 8192),
    })

    plan = compute_schedule(topo, vds, hosts)

    # All 4 VDs assigned
    total = sum(len(a.vd_names) for a in plan.assignments.values())
    assert total == 4

    # No single host should hold all 4
    assert max(len(a.vd_names) for a in plan.assignments.values()) < 4


def test_workers_preferred_before_master_when_available(topo_factory, vd_factory, host_factory):
    """Master should stay empty while workers can host the VDs."""
    nodes = {
        "cat9kv1": VDNode(name="cat9kv1", kind="cisco_cat9kv", image="cat9kv"),
        "c9800c1": VDNode(name="c9800c1", kind="cisco_cat9kv", image="c9800"),
    }
    topo = topo_factory(nodes=nodes, links=[], num_workers=2)

    vds = vd_factory({
        "cat9kv1": (4, 18432),
        "c9800c1": (4, 18432),
    })
    hosts = host_factory({
        "master":  (32, 131072),
        "worker1": (16, 65536),
        "worker2": (16, 65536),
    })

    plan = compute_schedule(topo, vds, hosts)

    assert plan.assignments["master"].vd_names == []
    assert len(plan.assignments["worker1"].vd_names) == 1
    assert len(plan.assignments["worker2"].vd_names) == 1


def test_sticky_preference_keeps_previous_host_when_it_fits(topo_factory, vd_factory, host_factory):
    nodes = {
        "r1": VDNode(name="r1", kind="linux", image="x"),
        "r2": VDNode(name="r2", kind="linux", image="x"),
    }
    topo = topo_factory(nodes=nodes, links=[], num_workers=2)
    vds = vd_factory({"r1": (2, 2048), "r2": (2, 2048)})
    hosts = host_factory({
        "master": (8, 8192),
        "worker1": (8, 8192),
        "worker2": (8, 8192),
    })

    plan = compute_schedule(
        topo,
        vds,
        hosts,
        placement_preferences={"r1": "worker2"},
    )

    assert plan.host_for_vd("r1") == "worker2"


def test_sticky_preference_is_ignored_when_host_cannot_fit(topo_factory, vd_factory, host_factory):
    nodes = {"r1": VDNode(name="r1", kind="linux", image="x")}
    topo = topo_factory(nodes=nodes, links=[], num_workers=2)
    vds = vd_factory({"r1": (8, 8192)})
    hosts = host_factory({
        "master": (8, 8192),
        "worker1": (2, 2048),
        "worker2": (8, 8192),
    })

    plan = compute_schedule(
        topo,
        vds,
        hosts,
        placement_preferences={"r1": "worker1"},
    )

    assert plan.host_for_vd("r1") == "worker2"


def test_infeasible_resources(topo_factory, vd_factory, host_factory):
    """Insufficient total resources should raise ScheduleError."""
    nodes = {f"R{i}": VDNode(name=f"R{i}", kind="linux", image="x") for i in range(1, 6)}
    topo = topo_factory(nodes=nodes, links=[], num_workers=1)

    vds = vd_factory({f"R{i}": (8, 16384) for i in range(1, 6)})  # needs 40 CPU, 80 GB
    hosts = host_factory({
        "master":  (4, 4096),
        "worker1": (4, 4096),
    })

    with pytest.raises(ScheduleError):
        compute_schedule(topo, vds, hosts)


def test_crosshost_link_detection(topo_factory, vd_factory, host_factory):
    """Links between VDs on different hosts should be classified as cross-host."""
    nodes = {
        "R1": VDNode(name="R1", kind="linux", image="x"),
        "R2": VDNode(name="R2", kind="linux", image="x"),
    }
    links = [Link(source="R1", source_iface="eth1", target="R2", target_iface="eth1")]
    topo = topo_factory(nodes=nodes, links=links, num_workers=1)

    # Force each VD on a different host by making them heavy
    vds = vd_factory({"R1": (8, 16384), "R2": (8, 16384)})
    hosts = host_factory({
        "master":  (8, 16384),
        "worker1": (8, 16384),
    })

    plan = compute_schedule(topo, vds, hosts)

    # One host per VD
    r1_host = plan.host_for_vd("R1")
    r2_host = plan.host_for_vd("R2")
    if r1_host != r2_host:
        assert len(plan.cross_host_links) == 1
        assert len(plan.local_links) == 0
    else:
        assert len(plan.cross_host_links) == 0
        assert len(plan.local_links) == 1
