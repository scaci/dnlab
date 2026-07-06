import asyncio
import struct
from types import SimpleNamespace

import pytest

import dnlab_multinode.services.follow_rabbit as follow_rabbit_svc
from dnlab_multinode.models.state import (
    DeploymentState,
    MgmtState,
    RealNetState,
    RuntimeLinkState,
)
from dnlab_multinode.services.follow_rabbit import (
    CapturePoint,
    FlowFilter,
    FollowRabbitError,
    FollowRabbitManager,
    FollowRabbitSession,
    build_bpf,
    build_capture_points,
    build_topo_graph,
    _observations_from_pcap,
    _remote_capture_timeout,
)
from dnlab_multinode.services.hosts_config import load_hosts_config
from dnlab_multinode.services.pcap_parse import IncrementalPcapParser


def test_build_bpf_forward_is_directional_five_tuple():
    flow = FlowFilter(
        src_ip="192.0.2.10",
        dst_ip="198.51.100.20",
        protocol="tcp",
        src_port=12345,
        dst_port=443,
    )

    assert build_bpf(flow) == (
        "src host 192.0.2.10 and dst host 198.51.100.20 and tcp "
        "and src port 12345 and dst port 443"
    )
    assert build_bpf(flow, "forward") == build_bpf(flow)


def test_build_bpf_return_swaps_ips_and_ports():
    flow = FlowFilter(
        src_ip="192.0.2.10",
        dst_ip="198.51.100.20",
        protocol="tcp",
        src_port=12345,
        dst_port=443,
    )

    assert build_bpf(flow, "return") == (
        "src host 198.51.100.20 and dst host 192.0.2.10 and tcp "
        "and src port 443 and dst port 12345"
    )


def test_build_bpf_icmp_forward_and_return_are_distinct_and_directional():
    flow = FlowFilter(src_ip="192.0.2.10", dst_ip="198.51.100.20", protocol="icmp")

    fwd = build_bpf(flow, "forward")
    ret = build_bpf(flow, "return")

    assert fwd == "src host 192.0.2.10 and dst host 198.51.100.20 and icmp"
    assert ret == "src host 198.51.100.20 and dst host 192.0.2.10 and icmp"
    assert fwd != ret
    # No symmetric `host X and host Y` that would match a reversed packet.
    assert "host 192.0.2.10 and host 198.51.100.20" not in fwd


def test_build_bpf_rejects_invalid_direction():
    with pytest.raises(FollowRabbitError):
        build_bpf(FlowFilter(src_ip="192.0.2.1", dst_ip="192.0.2.2"), "sideways")


def test_build_bpf_rejects_invalid_protocol_and_port_combo():
    with pytest.raises(FollowRabbitError):
        build_bpf(FlowFilter(src_ip="192.0.2.1", dst_ip="192.0.2.2", protocol="gre"))

    with pytest.raises(FollowRabbitError):
        build_bpf(FlowFilter(src_ip="192.0.2.1", dst_ip="192.0.2.2", protocol="icmp", dst_port=53))


def test_build_capture_points_include_runtime_mgmt_and_realnet():
    state = DeploymentState(lab_name="lab", topology_file="lab.yml")
    state.runtime_links = [
        RuntimeLinkState(
            id="l0",
            link_type="same_host",
            endpoint_a={"node": "r1", "iface": "eth1"},
            endpoint_b={"node": "r2", "iface": "eth1"},
            host_a="master",
            host_b="master",
            host_endpoint_a="r1-e1",
            host_endpoint_b="r2-e1",
        ),
        RuntimeLinkState(
            id="vx0",
            link_type="cross_host",
            endpoint_a={"node": "r2", "iface": "eth2"},
            endpoint_b={"node": "r3", "iface": "eth1"},
            host_a="master",
            host_b="worker1",
            host_endpoint_a="r2-e2",
            host_endpoint_b="r3-e1",
        ),
    ]
    state.scheduling = {"master": object(), "worker1": object()}
    state.mgmt = MgmtState(
        subnet="172.20.0.0/24",
        gateway="172.20.0.1",
        bridge="br-lab-mgmt",
        vrf="vrf-lab",
        vxlan_id=2000,
        vxlan_iface="vx-lab-mgmt",
    )
    state.realnets = [
        RealNetState(name="wan", bridge="br-wan", vxlan_id=4000, hosts=["master", "worker1"])
    ]

    points = build_capture_points(state)
    kinds = {p.link_type for p in points}

    assert {"same_host", "cross_host", "mgmt", "real_net"} <= kinds
    assert any(p.link_id == "l0" and p.iface == "r1-e1" for p in points)
    assert any(p.link_id == "vx0" and p.host == "worker1" for p in points)
    assert any(p.link_id == "mgmt:worker1" and p.iface == "vx-lab-mgmt" for p in points)
    assert any(p.link_id == "realnet:wan:master" for p in points)


def test_hosts_config_parses_follow_rabbit_default_and_override(tmp_path):
    hosts = tmp_path / "hosts.yml"
    hosts.write_text(
        """
infrastructure:
  master:
    host: 127.0.0.1
    ssh_user: root
  workers: {}
""",
        encoding="utf-8",
    )

    assert load_hosts_config(hosts).follow_the_rabbit.max_sessions == 1

    hosts.write_text(
        """
follow_the_rabbit:
  max_sessions: 3
infrastructure:
  master:
    host: 127.0.0.1
    ssh_user: root
  workers: {}
""",
        encoding="utf-8",
    )

    assert load_hosts_config(hosts).follow_the_rabbit.max_sessions == 3


def test_hosts_config_parses_legacy_plus_follow_rabbit(tmp_path):
    """Legacy hosts.yml using the pre-merge ``plus:`` block must still parse."""
    hosts = tmp_path / "hosts.yml"
    hosts.write_text(
        """
plus:
  follow_the_rabbit:
    max_sessions: 5
infrastructure:
  master:
    host: 127.0.0.1
    ssh_user: root
  workers: {}
""",
        encoding="utf-8",
    )

    assert load_hosts_config(hosts).follow_the_rabbit.max_sessions == 5


def test_session_serializes_hits_and_distributed_observations():
    session = FollowRabbitSession(
        session_id="s1",
        lab_name="lab",
        source_node="r1",
        flow=FlowFilter(src_ip="192.0.2.1", dst_ip="192.0.2.2"),
    )
    session.hits["l0"] = {"link_id": "l0", "host": "master"}
    session.observations.append({
        "capture_point": "l0:a:master:e1",
        "link_id": "l0",
        "host": "master",
        "iface": "e1",
        "rc": 0,
    })
    session.observations.append({
        "capture_point": "l0:b:worker1:e1",
        "link_id": "l0",
        "host": "worker1",
        "iface": "e1",
        "rc": 124,
    })

    data = session.to_dict()

    assert data["hits"] == [{"link_id": "l0", "host": "master"}]
    assert {o["host"] for o in data["observations"]} == {"master", "worker1"}
    assert data["completed_probe_count"] == 2
    assert data["packet_observation_count"] == 0
    assert data["completed_at"] is None


def test_build_topo_graph_maps_runtime_links_and_realnet_pseudo_node():
    state = DeploymentState(lab_name="lab", topology_file="lab.yml")
    state.runtime_links = [
        RuntimeLinkState(
            id="l0", link_type="same_host",
            endpoint_a={"node": "r1", "iface": "eth1"},
            endpoint_b={"node": "r2", "iface": "eth1"},
        ),
        RuntimeLinkState(
            id="rn0", link_type="real_net",
            endpoint_a={"node": "r2", "iface": "eth2"},
            endpoint_b={"real_net": "wan"},
        ),
    ]

    graph = build_topo_graph(state)

    assert graph.link_endpoints("l0") == ("r1", "r2")
    assert graph.link_endpoints("rn0") == ("r2", "realnet:wan")
    assert set(graph.candidate_links("r1")) == {"l0", "rn0"}


def _icmp_frame(ttl: int, src: str, dst: str, icmp_id: int, seq: int) -> bytes:
    icmp = struct.pack(">BBHHH", 8, 0, 0, icmp_id, seq)
    sb = bytes(int(p) for p in src.split("."))
    db = bytes(int(p) for p in dst.split("."))
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(icmp), 1, 0, ttl, 1, 0, sb, db)
    eth = b"\xaa" * 6 + b"\xbb" * 6 + struct.pack(">H", 0x0800)
    return eth + ip + icmp


def _pcap(*frames: bytes) -> bytes:
    out = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for frame in frames:
        out += struct.pack("<IIII", 1, 0, len(frame), len(frame)) + frame
    return out


def test_incremental_pcap_parser_buffers_partial_records():
    frame1 = _icmp_frame(64, "10.0.0.1", "10.0.0.2", 7, 1)
    frame2 = _icmp_frame(63, "10.0.0.1", "10.0.0.2", 7, 2)
    data = _pcap(frame1, frame2)
    parser = IncrementalPcapParser()

    assert parser.feed(data[:30]) == []
    first = parser.feed(data[30:24 + 16 + len(frame1)])
    rest = parser.feed(data[24 + 16 + len(frame1):])

    assert [p.icmp_seq for p in first] == [1]
    assert [p.icmp_seq for p in rest] == [2]


def test_capture_pipeline_reconstructs_oriented_path_from_pcap():
    """End-to-end glue: pcap bytes -> observations -> pure core -> reconstruction."""
    state = DeploymentState(lab_name="lab", topology_file="lab.yml")
    state.runtime_links = [
        RuntimeLinkState(id="l0", link_type="same_host",
                         endpoint_a={"node": "S", "iface": "eth1"},
                         endpoint_b={"node": "R1", "iface": "eth1"}),
        RuntimeLinkState(id="l1", link_type="same_host",
                         endpoint_a={"node": "R1", "iface": "eth2"},
                         endpoint_b={"node": "D", "iface": "eth1"}),
    ]
    p0 = CapturePoint(id="l0", link_id="l0", link_type="same_host", host="master", iface="e0")
    p1 = CapturePoint(id="l1", link_id="l1", link_type="same_host", host="master", iface="e1")

    session = FollowRabbitSession(
        session_id="s1", lab_name="lab", source_node="S",
        flow=FlowFilter(src_ip="10.0.0.1", dst_ip="10.0.0.2", protocol="icmp"),
    )
    session.graph = build_topo_graph(state)
    # Same flow seen on the near link with TTL 64 and the far link with TTL 63.
    for seq in (1, 2, 3):
        near = _pcap(_icmp_frame(64, "10.0.0.1", "10.0.0.2", 7, seq))
        far = _pcap(_icmp_frame(63, "10.0.0.1", "10.0.0.2", 7, seq))
        session.packet_obs += _observations_from_pcap(near, p0, "forward")
        session.packet_obs += _observations_from_pcap(far, p1, "forward")

    FollowRabbitManager()._reconstruct(session)

    fwd = session.reconstruction["forward"]
    assert fwd["classification"] == "certain"
    edges = {e["link_id"]: (e["src_node"], e["dst_node"])
             for layer in fwd["layers"] for e in layer["edges"]}
    assert edges == {"l0": ("S", "R1"), "l1": ("R1", "D")}
    by_link = {e["link_id"]: e for layer in fwd["layers"] for e in layer["edges"]}
    assert by_link["l0"]["source_iface"] == "eth1"
    assert by_link["l0"]["target_iface"] == "eth1"
    assert by_link["l0"]["endpoint_a"] == {"node": "S", "iface": "eth1"}
    assert by_link["l0"]["endpoint_b"] == {"node": "R1", "iface": "eth1"}
    assert by_link["l0"]["last_packet_at"] == 1
    assert session.reconstruction["asymmetric"] is False


def test_reconstruction_serializes_parallel_physical_links_distinctly():
    state = DeploymentState(lab_name="lab", topology_file="lab.yml")
    state.runtime_links = [
        RuntimeLinkState(id="l0", link_type="same_host",
                         endpoint_a={"node": "n9kv1", "iface": "eth1"},
                         endpoint_b={"node": "n9kv2", "iface": "eth1"}),
        RuntimeLinkState(id="l1", link_type="same_host",
                         endpoint_a={"node": "n9kv1", "iface": "eth2"},
                         endpoint_b={"node": "n9kv2", "iface": "eth2"}),
    ]
    point = CapturePoint(id="l1", link_id="l1", link_type="same_host", host="master", iface="host-l1")
    session = FollowRabbitSession(
        session_id="s1", lab_name="lab", source_node="n9kv1",
        flow=FlowFilter(src_ip="10.0.0.1", dst_ip="10.0.0.2", protocol="icmp"),
    )
    session.graph = build_topo_graph(state)
    session.packet_obs += _observations_from_pcap(
        _pcap(_icmp_frame(64, "10.0.0.1", "10.0.0.2", 7, 1)),
        point,
        "forward",
    )

    FollowRabbitManager()._reconstruct(session)

    edges = [e for layer in session.reconstruction["forward"]["layers"] for e in layer["edges"]]
    assert [e["link_id"] for e in edges] == ["l1"]
    assert edges[0]["source_iface"] == "eth2"
    assert edges[0]["target_iface"] == "eth2"
    assert edges[0]["last_packet_at"] == 1


def test_remote_capture_timeout_expires_before_session_timeout():
    assert _remote_capture_timeout(60) == 58
    assert _remote_capture_timeout(2) == 1


def test_finish_waits_grace_for_probe_output(monkeypatch):
    async def run():
        monkeypatch.setattr(follow_rabbit_svc, "CAPTURE_DRAIN_GRACE_SECONDS", 0.05)

        async def fake_run_tcpdump_stream(point, cfg, bpf, timeout_seconds, on_packet):
            assert timeout_seconds == 1
            await asyncio.sleep(0.02)
            for pkt in IncrementalPcapParser().feed(
                _pcap(_icmp_frame(64, "10.0.0.1", "10.0.0.2", 7, 1))
            ):
                await on_packet(pkt)
            return 124, "tcpdump timed out after flushing one packet"

        monkeypatch.setattr(follow_rabbit_svc, "_run_tcpdump_stream", fake_run_tcpdump_stream)

        manager = FollowRabbitManager()
        session = FollowRabbitSession(
            session_id="s1",
            lab_name="lab",
            source_node="S",
            flow=FlowFilter(src_ip="10.0.0.1", dst_ip="10.0.0.2", protocol="icmp"),
            timeout_seconds=0.01,
            capture_points=1,
            probe_count=1,
        )
        point = CapturePoint(id="l0", link_id="l0", link_type="same_host", host="master", iface="e0")
        cfg = SimpleNamespace(all_hosts={})
        events = []
        watch = asyncio.create_task(
            manager._watch_point(session, point, cfg, "icmp", "forward", events.append)
        )
        finish = asyncio.create_task(manager._finish_after_timeout(session, events.append))
        manager._sessions[session.session_id] = session
        manager._tasks[session.session_id] = [watch, finish]

        await finish

        assert session.status == "done"
        assert session.completed_at
        assert len(session.observations) == 1
        assert session.observations[0]["packet_count"] == 1
        assert len(session.packet_obs) == 1
        assert events[-1]["event"] == "session_done"
        assert events[-1]["data"]["completed_probe_count"] == 1
        assert events[-1]["data"]["packet_observation_count"] == 1
        assert any(evt["event"] == "session_progress" for evt in events)

    asyncio.run(run())


def test_finish_completes_early_when_all_probes_are_done():
    async def run():
        manager = FollowRabbitManager()
        session = FollowRabbitSession(
            session_id="s1",
            lab_name="lab",
            source_node="S",
            flow=FlowFilter(src_ip="10.0.0.1", dst_ip="10.0.0.2", protocol="icmp"),
            timeout_seconds=10,
            capture_points=1,
            probe_count=1,
        )

        async def completed_probe():
            await asyncio.sleep(0.01)

        events = []
        probe = asyncio.create_task(completed_probe())
        finish = asyncio.create_task(manager._finish_after_timeout(session, events.append))
        manager._sessions[session.session_id] = session
        manager._tasks[session.session_id] = [probe, finish]

        await asyncio.wait_for(finish, timeout=0.5)

        assert session.status == "done"
        assert session.completed_at
        assert events[-1]["event"] == "session_done"

    asyncio.run(run())


def test_finish_cancels_probe_after_grace(monkeypatch):
    async def run():
        monkeypatch.setattr(follow_rabbit_svc, "CAPTURE_DRAIN_GRACE_SECONDS", 0.01)
        manager = FollowRabbitManager()
        session = FollowRabbitSession(
            session_id="s1",
            lab_name="lab",
            source_node="S",
            flow=FlowFilter(src_ip="10.0.0.1", dst_ip="10.0.0.2", protocol="icmp"),
            timeout_seconds=0.01,
            capture_points=1,
            probe_count=1,
        )
        cleanup_seen = asyncio.Event()

        async def stuck_probe():
            try:
                await asyncio.sleep(60)
            finally:
                cleanup_seen.set()

        events = []
        probe = asyncio.create_task(stuck_probe())
        finish = asyncio.create_task(manager._finish_after_timeout(session, events.append))
        manager._sessions[session.session_id] = session
        manager._tasks[session.session_id] = [probe, finish]

        await finish

        assert session.status == "done"
        assert cleanup_seen.is_set()
        assert probe.cancelled()
        assert events[-1]["event"] == "session_done"
        assert events[-1]["data"]["completed_probe_count"] == 0

    asyncio.run(run())


def test_stop_marks_session_and_awaits_capture_cleanup():
    async def run():
        manager = FollowRabbitManager()
        session = FollowRabbitSession(
            session_id="s1",
            lab_name="lab",
            source_node="r1",
            flow=FlowFilter(src_ip="192.0.2.1", dst_ip="192.0.2.2"),
        )
        cleanup_seen = asyncio.Event()

        async def capture():
            try:
                await asyncio.sleep(60)
            finally:
                cleanup_seen.set()

        manager._sessions[session.session_id] = session
        manager._tasks[session.session_id] = [asyncio.create_task(capture())]
        await asyncio.sleep(0)

        events = []
        result = await manager.stop(session.session_id, events.append)

        assert result["status"] == "stopped"
        assert cleanup_seen.is_set()
        assert session.session_id not in manager._tasks
        assert events[-1]["event"] == "session_done"

    asyncio.run(run())
