"""Follow the Rabbit core — the five binary success criteria, plus the pcap
parser, exercised entirely with synthetic observations (no real captures)."""

from __future__ import annotations

import struct

from dnlab_multinode.services.flow_reconstruct import (
    LinkRef,
    PacketObservation,
    TopoGraph,
    instance_key,
    reconstruct,
)
from dnlab_multinode.services.pcap_parse import parse_pcap


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def graph(*links: tuple[str, str, str]) -> TopoGraph:
    return TopoGraph(links=[LinkRef(lid, a, b) for lid, a, b in links])


def obs(link_id, ttl, key, direction="forward", host="h", ts=0.0):
    return PacketObservation(
        link_id=link_id,
        ttl=ttl,
        ts_local=ts,
        host=host,
        direction=direction,
        instance_key=key,
        five_tuple=("10.0.0.1", "10.0.0.2", "icmp", 0, 0),
    )


def edges_of(path):
    return {e.link_id: (e.src_node, e.dst_node) for layer in path.layers for e in layer.edges}


def weights_of(path):
    return {e.link_id: round(e.weight, 4) for layer in path.layers for e in layer.edges}


# --------------------------------------------------------------------------- #
# Criterion 1 — linear ICMP → CERTAIN, nodes in order, weights 1.0
# --------------------------------------------------------------------------- #
def test_linear_icmp_is_certain_oriented_and_unit_weighted():
    g = graph(("L0", "S", "R1"), ("L1", "R1", "R2"), ("L2", "R2", "D"))
    observations = []
    for i in range(5):
        key = f"icmp:1:{i}"
        observations.append(obs("L0", 64, key))
        observations.append(obs("L1", 63, key))
        observations.append(obs("L2", 62, key))

    result = reconstruct(g, observations, "S")
    fwd = result.forward

    assert fwd.classification == "certain"
    assert [l.ttl for l in fwd.layers] == [64, 63, 62]
    assert edges_of(fwd) == {"L0": ("S", "R1"), "L1": ("R1", "R2"), "L2": ("R2", "D")}
    assert all(w == 1.0 for w in weights_of(fwd).values())
    assert all(e.weight_quality == "joint" for layer in fwd.layers for e in layer.edges)
    # No reply traffic captured → empty backward leg, not an error.
    assert fwd.layers and not result.backward.layers
    assert result.asymmetric is False


def test_same_ttl_l2_chain_is_determinate_not_ecmp():
    g = graph(
        ("vx3", "n9kv1", "cumulu1"),
        ("vx0", "n9kv1", "n9kv2"),
        ("vx2", "n9kv2", "vjunos1"),
        ("rn0", "vjunos1", "realnet:net1"),
    )
    observations = []
    for i in range(4):
        key = f"icmp:1:{i}"
        observations.append(obs("vx3", 64, key, direction="forward"))
        observations.append(obs("vx0", 64, key, direction="forward"))
        observations.append(obs("vx2", 64, key, direction="forward"))
        observations.append(obs("rn0", 63, key, direction="forward"))
        observations.append(obs("rn0", 63, key, direction="return"))
        observations.append(obs("vx2", 62, key, direction="return"))
        observations.append(obs("vx0", 62, key, direction="return"))
        observations.append(obs("vx3", 62, key, direction="return"))

    result = reconstruct(g, observations, "cumulu1")

    assert result.forward.classification == "certain"
    assert {layer.ttl: layer.state for layer in result.forward.layers} == {
        64: "determinate",
        63: "determinate",
    }
    assert edges_of(result.forward) == {
        "vx3": ("cumulu1", "n9kv1"),
        "vx0": ("n9kv1", "n9kv2"),
        "vx2": ("n9kv2", "vjunos1"),
        "rn0": ("vjunos1", "realnet:net1"),
    }
    assert result.backward.classification == "certain"
    assert {layer.ttl: layer.state for layer in result.backward.layers} == {
        63: "determinate",
        62: "determinate",
    }
    assert edges_of(result.backward) == {
        "rn0": ("realnet:net1", "vjunos1"),
        "vx2": ("vjunos1", "n9kv2"),
        "vx0": ("n9kv2", "n9kv1"),
        "vx3": ("n9kv1", "cumulu1"),
    }
    assert result.asymmetric is False


# --------------------------------------------------------------------------- #
# Criterion 2 — ECMP 4/7 vs 3/7 → MULTIPATH; JOINT with keys, MARGINAL without
# --------------------------------------------------------------------------- #
def _ecmp_graph():
    return graph(
        ("L0", "S", "R1"),
        ("L1a", "R1", "R2a"), ("L1b", "R1", "R2b"),
        ("L2a", "R2a", "R3"), ("L2b", "R2b", "R3"),
        ("L3", "R3", "D"),
    )


def _ecmp_obs(keyed: bool):
    observations = []
    for i in range(7):
        key = f"icmp:1:{i}" if keyed else None
        observations.append(obs("L0", 64, key))
        if i < 4:
            observations.append(obs("L1a", 63, key))
            observations.append(obs("L2a", 62, key))
        else:
            observations.append(obs("L1b", 63, key))
            observations.append(obs("L2b", 62, key))
        observations.append(obs("L3", 61, key))
    return observations


def test_ecmp_joint_when_instances_thread():
    result = reconstruct(_ecmp_graph(), _ecmp_obs(keyed=True), "S")
    fwd = result.forward

    assert fwd.classification == "multipath"
    assert weights_of(fwd) == {
        "L0": 1.0,
        "L1a": round(4 / 7, 4), "L1b": round(3 / 7, 4),
        "L2a": round(4 / 7, 4), "L2b": round(3 / 7, 4),
        "L3": 1.0,
    }
    states = {l.ttl: l.state for l in fwd.layers}
    assert states == {64: "determinate", 63: "ecmp", 62: "ecmp", 61: "determinate"}
    assert all(e.weight_quality == "joint" for layer in fwd.layers for e in layer.edges)


def test_ecmp_marginal_when_instances_not_correlatable():
    result = reconstruct(_ecmp_graph(), _ecmp_obs(keyed=False), "S")
    fwd = result.forward

    assert fwd.classification == "multipath"
    assert weights_of(fwd)["L1a"] == round(4 / 7, 4)
    assert weights_of(fwd)["L1b"] == round(3 / 7, 4)
    assert all(e.weight_quality == "marginal" for layer in fwd.layers for e in layer.edges)


# --------------------------------------------------------------------------- #
# Criterion 3 — missing intermediate layer → PARTIAL, missing_ttl localized
# --------------------------------------------------------------------------- #
def test_missing_layer_is_partial_and_localized():
    g = graph(
        ("L0", "S", "R1"), ("L1", "R1", "R2"),
        ("L2", "R2", "R3"), ("L3", "R3", "D"),
    )
    observations = []
    for i in range(3):
        key = f"icmp:1:{i}"
        observations.append(obs("L0", 64, key))
        # L1 at TTL 63 is dropped — capture missing at that layer.
        observations.append(obs("L2", 62, key))
        observations.append(obs("L3", 61, key))

    fwd = reconstruct(g, observations, "S").forward

    assert fwd.classification == "partial"
    gap = [s for s in fwd.segments if s.state == "unresolved"]
    assert len(gap) == 1
    assert gap[0].ttl_high == 64 and gap[0].ttl_low == 62
    assert gap[0].missing_ttl == 63
    # The resolved suffix is still oriented correctly.
    assert edges_of(fwd)["L2"] == ("R2", "R3")
    assert edges_of(fwd)["L3"] == ("R3", "D")


# --------------------------------------------------------------------------- #
# Criterion 4 — same machine for TCP (seq) and UDP (IP ID / marginal)
# --------------------------------------------------------------------------- #
def test_tcp_and_udp_reuse_the_same_core_without_protocol_branches():
    g = graph(("L0", "S", "R1"), ("L1", "R1", "D"))

    tcp = []
    for seq in (1000, 2000, 3000):
        key = instance_key("tcp", tcp_seq=seq, payload_len=100, ip_id=5)
        assert key is not None
        tcp.append(obs("L0", 64, key))
        tcp.append(obs("L1", 63, key))
    tcp_fwd = reconstruct(g, tcp, "S").forward
    assert tcp_fwd.classification == "certain"
    assert edges_of(tcp_fwd) == {"L0": ("S", "R1"), "L1": ("R1", "D")}

    # UDP with a usable IP ID → correlatable (joint).
    udp_keyed = []
    for ipid in (11, 22, 33):
        key = instance_key("udp", ip_id=ipid)
        assert key is not None
        udp_keyed.append(obs("L0", 64, key))
        udp_keyed.append(obs("L1", 63, key))
    udp_fwd = reconstruct(g, udp_keyed, "S").forward
    assert udp_fwd.classification == "certain"
    assert all(e.weight_quality == "joint" for layer in udp_fwd.layers for e in layer.edges)

    # UDP with IP ID zeroed (DF, RFC 6864) and no payload hash → not correlatable.
    assert instance_key("udp", ip_id=0) is None
    udp_marg = []
    for _ in range(3):
        udp_marg.append(obs("L0", 64, None))
        udp_marg.append(obs("L1", 63, None))
    udp_marg_fwd = reconstruct(g, udp_marg, "S").forward
    assert udp_marg_fwd.classification == "certain"
    assert all(e.weight_quality == "marginal" for layer in udp_marg_fwd.layers for e in layer.edges)
    # Even uncorrelatable, the per-hop path is still exact.
    assert edges_of(udp_marg_fwd) == {"L0": ("S", "R1"), "L1": ("R1", "D")}


# --------------------------------------------------------------------------- #
# Criterion 5 — forward and return independent; one case asymmetric
# --------------------------------------------------------------------------- #
def test_forward_and_return_reconstructed_independently_with_asymmetry():
    # Forward S->R1->R2->D; return D->R2->R3->S reuses one link, differs in the
    # middle → routing asymmetry emerges naturally.
    g = graph(
        ("F0", "S", "R1"), ("F1", "R1", "R2"), ("F2", "R2", "D"),
        ("M", "R2", "R3"), ("N", "R3", "S"),
    )
    observations = []
    for i in range(2):
        key = f"icmp:1:{i}"
        observations.append(obs("F0", 64, key, direction="forward"))
        observations.append(obs("F1", 63, key, direction="forward"))
        observations.append(obs("F2", 62, key, direction="forward"))
        observations.append(obs("F2", 64, key, direction="return"))
        observations.append(obs("M", 63, key, direction="return"))
        observations.append(obs("N", 62, key, direction="return"))

    result = reconstruct(g, observations, "S")

    assert edges_of(result.forward) == {"F0": ("S", "R1"), "F1": ("R1", "R2"), "F2": ("R2", "D")}
    assert edges_of(result.backward) == {"F2": ("D", "R2"), "M": ("R2", "R3"), "N": ("R3", "S")}
    assert result.forward.classification == "certain"
    assert result.backward.classification == "certain"
    assert result.asymmetric is True


def test_symmetric_routing_is_not_flagged_asymmetric():
    g = graph(("F0", "S", "R1"), ("F1", "R1", "R2"), ("F2", "R2", "D"))
    observations = []
    for i in range(2):
        key = f"icmp:1:{i}"
        observations.append(obs("F0", 64, key, direction="forward"))
        observations.append(obs("F1", 63, key, direction="forward"))
        observations.append(obs("F2", 62, key, direction="forward"))
        observations.append(obs("F2", 64, key, direction="return"))
        observations.append(obs("F1", 63, key, direction="return"))
        observations.append(obs("F0", 62, key, direction="return"))

    result = reconstruct(g, observations, "S")
    assert result.asymmetric is False
    assert edges_of(result.backward) == {"F2": ("D", "R2"), "F1": ("R2", "R1"), "F0": ("R1", "S")}


# --------------------------------------------------------------------------- #
# pcap parser — binary round-trip into the fields the core consumes
# --------------------------------------------------------------------------- #
def _icmp_echo_pcap() -> bytes:
    icmp = struct.pack(">BBHHH", 8, 0, 0, 0xABCD, 0x0007) + b"payload!"
    ip = struct.pack(
        ">BBHHHBBH4s4s",
        0x45, 0, 20 + len(icmp), 0x1234, 0, 64, 1, 0,
        bytes([10, 0, 0, 1]), bytes([10, 0, 0, 2]),
    )
    eth = b"\xaa" * 6 + b"\xbb" * 6 + struct.pack(">H", 0x0800)
    frame = eth + ip + icmp
    record = struct.pack("<IIII", 1, 2, len(frame), len(frame)) + frame
    global_header = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    return global_header + record


def test_pcap_parser_extracts_ttl_ipid_and_icmp_instance_fields():
    packets = parse_pcap(_icmp_echo_pcap())
    assert len(packets) == 1
    pkt = packets[0]
    assert pkt.proto == "icmp"
    assert pkt.src_ip == "10.0.0.1" and pkt.dst_ip == "10.0.0.2"
    assert pkt.ttl == 64
    assert pkt.ip_id == 0x1234
    assert pkt.icmp_id == 0xABCD and pkt.icmp_seq == 0x0007
    assert instance_key("icmp", icmp_id=pkt.icmp_id, icmp_seq=pkt.icmp_seq) == "icmp:43981:7"


def test_pcap_parser_ignores_truncated_or_foreign_data():
    assert parse_pcap(b"") == []
    assert parse_pcap(b"not a pcap file at all") == []
