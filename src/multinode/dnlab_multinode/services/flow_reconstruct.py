"""Follow the Rabbit — passive TTL-driven flow reconstruction core.

This module is a **pure** function library: no SSH, no PCAP, no JSON, no clock.
It takes a topology graph plus a list of already-parsed packet observations and
reconstructs the oriented per-hop DAG of the flow.

The direction of a flow is **not measured, it is deduced from the TTL**: every L3
hop decrements the TTL by one, while bridges and VXLAN are transparent at L2 and
do not. So the TTL decreases monotonically along the path and "decreasing" *is*
the direction. The initial TTL is never assumed (no 64/128/255 hardcoding): the
anchor is the *node* (``source_node``), and the layers are derived from whatever
TTL values are actually observed.

The instance key correlates a packet across hops (it is invariant along the
path); the TTL orders the hops (it mutates). One correlates, the other orders —
they are never conflated.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# Data contract — observation input
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PacketObservation:
    link_id: str             # id of the link in the graph (RuntimeLinkState.id)
    ttl: int                 # TTL/hop-limit observed on THIS link
    ts_local: float          # capturing host clock; ONLY an intra-host tie-break
    host: str                # capturing host (for TTL ties on a shared clock)
    direction: str           # "forward" | "return" (which matcher took it)
    instance_key: str | None  # per-instance identity; None if not correlatable
    five_tuple: tuple        # (src_ip, dst_ip, proto, src_port, dst_port) observed


# --------------------------------------------------------------------------- #
# Data contract — topology graph
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LinkRef:
    link_id: str
    node_a: str
    node_b: str
    iface_a: str = ""
    iface_b: str = ""


@dataclass
class TopoGraph:
    nodes: set[str] = field(default_factory=set)
    links: list[LinkRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_id: dict[str, LinkRef] = {l.link_id: l for l in self.links}
        self._adj: dict[str, list[LinkRef]] = defaultdict(list)
        for link in self.links:
            self.nodes.add(link.node_a)
            self.nodes.add(link.node_b)
            self._adj[link.node_a].append(link)
            self._adj[link.node_b].append(link)

    def link_endpoints(self, link_id: str) -> tuple[str, str]:
        link = self._by_id[link_id]
        return (link.node_a, link.node_b)

    def neighbors(self, node: str) -> set[str]:
        out: set[str] = set()
        for link in self._adj.get(node, []):
            out.add(link.node_b if link.node_a == node else link.node_a)
        return out

    def candidate_links(self, source: str) -> list[str]:
        """BFS from ``source`` → every link reachable on the connected graph."""
        if source not in self.nodes:
            return []
        seen_nodes = {source}
        queue = [source]
        link_ids: list[str] = []
        link_seen: set[str] = set()
        while queue:
            node = queue.pop(0)
            for link in self._adj.get(node, []):
                if link.link_id not in link_seen:
                    link_seen.add(link.link_id)
                    link_ids.append(link.link_id)
                other = link.node_b if link.node_a == node else link.node_a
                if other not in seen_nodes:
                    seen_nodes.add(other)
                    queue.append(other)
        return link_ids


# --------------------------------------------------------------------------- #
# Data contract — result (oriented per-TTL-layer DAG)
# --------------------------------------------------------------------------- #
@dataclass
class FlowEdge:
    link_id: str
    src_node: str            # oriented high TTL -> low TTL
    dst_node: str
    ttl: int
    weight: float            # fraction of the flow's instances/packets on the link
    weight_quality: str      # "joint" | "marginal"


@dataclass
class FlowLayer:
    ttl: int
    edges: list[FlowEdge]
    state: str               # "determinate" | "ecmp" | "unresolved"


@dataclass
class FlowSegment:            # boundary between two adjacent layers
    ttl_high: int
    ttl_low: int
    state: str               # "determinate" | "ecmp" | "unresolved"
    missing_ttl: int | None  # missing layer localized by the contiguity gate


@dataclass
class FlowPath:              # one leg (forward or return)
    leg: str                 # "forward" | "return"
    layers: list[FlowLayer]  # ordered TTL max -> min
    segments: list[FlowSegment]
    classification: str      # "certain" | "multipath" | "partial"


@dataclass
class ReconstructionResult:
    forward: FlowPath
    backward: FlowPath
    asymmetric: bool         # forward and backward are not mirror images


# --------------------------------------------------------------------------- #
# Instance key — protocol-specific, with graceful degradation
# --------------------------------------------------------------------------- #
def instance_key(
    proto: str,
    *,
    icmp_id: int | None = None,
    icmp_seq: int | None = None,
    tcp_seq: int | None = None,
    payload_len: int | None = None,
    ip_id: int | None = None,
    payload_hash: str | None = None,
) -> str | None:
    """Identity that distinguishes packet *k* from *k+1* inside one flow.

    The key is **invariant** along the path (it must not be a field a router
    rewrites). Returns ``None`` when no stable per-instance identity exists, in
    which case the flow is still reconstructable but only at MARGINAL quality.
    """
    proto = (proto or "").lower()
    if proto in {"icmp", "icmp6", "icmpv6"}:
        if icmp_id is None or icmp_seq is None:
            return None
        return f"icmp:{icmp_id}:{icmp_seq}"
    if proto == "tcp":
        if tcp_seq is None:
            return None
        # seq separates packets; payload_len + IP ID separate retransmissions
        # that share the same sequence number.
        return f"tcp:{tcp_seq}:{payload_len if payload_len is not None else ''}:{ip_id or 0}"
    if proto == "udp":
        # IP ID is reliable only when not zeroed. Modern kernels zero it with DF
        # (RFC 6864); the capture layer decides at runtime whether to pass it.
        if ip_id:
            return f"udp:ipid:{ip_id}"
        if payload_hash:
            return f"udp:hash:{payload_hash}"
        return None
    return None


# --------------------------------------------------------------------------- #
# Reconstruction
# --------------------------------------------------------------------------- #
def reconstruct(
    graph: TopoGraph,
    observations: list[PacketObservation],
    source_node: str,
) -> ReconstructionResult:
    """Run the same machine twice, independently, for forward and return."""
    forward_obs = [o for o in observations if o.direction == "forward"]
    return_obs = [o for o in observations if o.direction == "return"]
    forward = _reconstruct_leg(graph, forward_obs, "forward", source_node)
    backward = _reconstruct_leg(graph, return_obs, "return", source_node)
    return ReconstructionResult(
        forward=forward,
        backward=backward,
        asymmetric=_is_asymmetric(forward, backward),
    )


def _reconstruct_leg(
    graph: TopoGraph,
    obs: list[PacketObservation],
    leg: str,
    source_node: str,
) -> FlowPath:
    if not obs:
        # Empty return leg (no replies) is a valid empty path, not an error.
        return FlowPath(leg=leg, layers=[], segments=[], classification="certain")

    obs_by_link: dict[str, list[PacketObservation]] = defaultdict(list)
    for o in obs:
        obs_by_link[o.link_id].append(o)

    # Representative TTL per link (mode over its observations).
    link_ttl: dict[str, int] = {
        lid: Counter(o.ttl for o in lobs).most_common(1)[0][0]
        for lid, lobs in obs_by_link.items()
    }

    # BIN by TTL → the layers.
    layer_links: dict[int, list[str]] = defaultdict(list)
    for lid, ttl in link_ttl.items():
        layer_links[ttl].append(lid)
    ttls_desc = sorted(layer_links, reverse=True)
    max_ttl, min_ttl = ttls_desc[0], ttls_desc[-1]

    # Weighting: joint when every observation carries an instance key (threading
    # possible), marginal otherwise. Either way each instance/packet of the flow
    # is counted once per link it touched.
    keyed = all(o.instance_key is not None for o in obs)
    quality = "joint" if keyed else "marginal"
    if keyed:
        total = len({o.instance_key for o in obs}) or 1

        def count(lid: str) -> int:
            return len({o.instance_key for o in obs_by_link[lid]})
    else:
        total = sum(len(obs_by_link[lid]) for lid in layer_links[max_ttl]) or 1

        def count(lid: str) -> int:
            return len(obs_by_link[lid])

    # ORIENT high TTL -> low TTL, per contiguous run of TTL values.
    oriented = _orient(graph, layer_links, ttls_desc, source_node)

    # Build layers (TTL max -> min).
    layers: list[FlowLayer] = []
    for ttl in ttls_desc:
        lids = layer_links[ttl]
        edges = [
            FlowEdge(
                link_id=lid,
                src_node=oriented[lid][0] or "",
                dst_node=oriented[lid][1] or "",
                ttl=ttl,
                weight=count(lid) / total,
                weight_quality=quality,
            )
            for lid in lids
        ]
        if any(oriented[lid][0] is None for lid in lids):
            state = "unresolved"
        elif _ttl_layer_is_ecmp(lids, oriented):
            state = "ecmp"
        else:
            state = "determinate"
        layers.append(FlowLayer(ttl=ttl, edges=edges, state=state))

    # Per-segment state between consecutive present layers (the gates).
    segments = _segments(graph, layer_links, ttls_desc, oriented)

    # ANCHOR gate: forward anchors at the max-TTL end on source_node; the return
    # leg ends at source_node, so it anchors at the min-TTL end.
    anchor_ttl = max_ttl if leg == "forward" else min_ttl
    anchor_ok = any(
        source_node in graph.link_endpoints(lid) for lid in layer_links[anchor_ttl]
    )

    classification = _classify(layers, segments, anchor_ok)
    return FlowPath(leg=leg, layers=layers, segments=segments, classification=classification)


def _orient(
    graph: TopoGraph,
    layer_links: dict[int, list[str]],
    ttls_desc: list[int],
    source_node: str,
) -> dict[str, tuple[str | None, str | None]]:
    """Orient every link high TTL -> low TTL.

    Orientation is structural: within a contiguous run of TTL values the node a
    link shares with the next-lower layer is its downstream node. ``source_node``
    only seeds a single-layer first run (a directly connected hop).
    """
    oriented: dict[str, tuple[str | None, str | None]] = {}
    runs = _contiguous_runs(ttls_desc)
    for run in runs:
        run_desc = [(ttl, layer_links[ttl]) for ttl in run]
        upstream = _initial_upstream(run_desc, graph, source_node)
        for _ttl, lids in run_desc:
            layer_oriented, downstream = _orient_same_ttl_links(graph, lids, upstream)
            oriented.update(layer_oriented)
            upstream = downstream or upstream
    return oriented


def _orient_same_ttl_links(
    graph: TopoGraph,
    lids: list[str],
    upstream: set[str],
) -> tuple[dict[str, tuple[str | None, str | None]], set[str]]:
    """Orient a same-TTL L2 segment away from the current upstream nodes."""
    remaining = set(lids)
    frontier = list(upstream)
    oriented: dict[str, tuple[str | None, str | None]] = {}
    downstream: set[str] = set()

    while frontier:
        node = frontier.pop(0)
        for lid in list(remaining):
            a, b = graph.link_endpoints(lid)
            if node == a:
                src, dst = a, b
            elif node == b:
                src, dst = b, a
            else:
                continue
            oriented[lid] = (src, dst)
            remaining.remove(lid)
            downstream.add(dst)
            frontier.append(dst)

    for lid in remaining:
        oriented[lid] = (None, None)
    return oriented, downstream


def _initial_upstream(
    run_desc: list[tuple[int, list[str]]],
    graph: TopoGraph,
    source_node: str,
) -> set[str]:
    """Upstream node set seeding a contiguous run's top layer."""
    top_nodes = _layer_nodes(run_desc[0][1], graph)
    if source_node in top_nodes:
        return {source_node}
    if len(run_desc) >= 2:
        second_nodes = _layer_nodes(run_desc[1][1], graph)
        upstream = top_nodes - (top_nodes & second_nodes)
        if upstream:
            return upstream
    # Single-layer run or no structural cue: fall back to the topology anchor.
    if source_node in top_nodes:
        return {source_node}
    return set()


def _layer_nodes(lids: list[str], graph: TopoGraph) -> set[str]:
    nodes: set[str] = set()
    for lid in lids:
        a, b = graph.link_endpoints(lid)
        nodes.add(a)
        nodes.add(b)
    return nodes


def _contiguous_runs(ttls_desc: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    current = [ttls_desc[0]]
    for ttl in ttls_desc[1:]:
        if current[-1] - ttl == 1:
            current.append(ttl)
        else:
            runs.append(current)
            current = [ttl]
    runs.append(current)
    return runs


def _segments(
    graph: TopoGraph,
    layer_links: dict[int, list[str]],
    ttls_desc: list[int],
    oriented: dict[str, tuple[str | None, str | None]],
) -> list[FlowSegment]:
    segments: list[FlowSegment] = []
    for hi, lo in zip(ttls_desc, ttls_desc[1:]):
        if hi - lo > 1:
            # CONTIGUITY gate failed: a missing capture at layer hi-1.
            segments.append(FlowSegment(ttl_high=hi, ttl_low=lo, state="unresolved", missing_ttl=hi - 1))
            continue
        if not _adjacency_ok(layer_links[hi], layer_links[lo], oriented):
            # ADJACENCY gate failed.
            segments.append(FlowSegment(ttl_high=hi, ttl_low=lo, state="unresolved", missing_ttl=None))
            continue
        ecmp = (
            _ttl_layer_is_ecmp(layer_links[hi], oriented)
            or _ttl_layer_is_ecmp(layer_links[lo], oriented)
        )
        segments.append(FlowSegment(
            ttl_high=hi,
            ttl_low=lo,
            state="ecmp" if ecmp else "determinate",
            missing_ttl=None,
        ))
    return segments


def _adjacency_ok(
    hi_lids: list[str],
    lo_lids: list[str],
    oriented: dict[str, tuple[str | None, str | None]],
) -> bool:
    hi_exits = _layer_exit_nodes(hi_lids, oriented)
    lo_entries = _layer_entry_nodes(lo_lids, oriented)
    return bool(hi_exits) and bool(lo_entries) and lo_entries <= hi_exits


def _layer_entry_nodes(
    lids: list[str],
    oriented: dict[str, tuple[str | None, str | None]],
) -> set[str]:
    srcs = {oriented[lid][0] for lid in lids if oriented[lid][0] is not None}
    dsts = {oriented[lid][1] for lid in lids if oriented[lid][1] is not None}
    return {node for node in srcs if node not in dsts}


def _layer_exit_nodes(
    lids: list[str],
    oriented: dict[str, tuple[str | None, str | None]],
) -> set[str]:
    srcs = {oriented[lid][0] for lid in lids if oriented[lid][0] is not None}
    dsts = {oriented[lid][1] for lid in lids if oriented[lid][1] is not None}
    return {node for node in dsts if node not in srcs}


def _ttl_layer_is_ecmp(
    lids: list[str],
    oriented: dict[str, tuple[str | None, str | None]],
) -> bool:
    if len(lids) <= 1:
        return False
    indegree: Counter[str] = Counter()
    outdegree: Counter[str] = Counter()
    nodes: set[str] = set()
    for lid in lids:
        src, dst = oriented[lid]
        if src is None or dst is None:
            return False
        nodes.add(src)
        nodes.add(dst)
        outdegree[src] += 1
        indegree[dst] += 1
    sources = [n for n in nodes if indegree[n] == 0 and outdegree[n] > 0]
    sinks = [n for n in nodes if outdegree[n] == 0 and indegree[n] > 0]
    if len(sources) != 1 or len(sinks) != 1:
        return True
    return any(indegree[n] > 1 or outdegree[n] > 1 for n in nodes)


def _classify(layers: list[FlowLayer], segments: list[FlowSegment], anchor_ok: bool) -> str:
    if (
        not anchor_ok
        or any(l.state == "unresolved" for l in layers)
        or any(s.state == "unresolved" for s in segments)
    ):
        return "partial"
    if any(l.state == "ecmp" for l in layers):
        return "multipath"
    return "certain"


def _is_asymmetric(forward: FlowPath, backward: FlowPath) -> bool:
    """Two legs are asymmetric when both exist and are not mirror images."""
    fwd = _edge_map(forward)
    bwd = _edge_map(backward)
    if not fwd or not bwd:
        # A missing reply leg is "no return traffic", not asymmetric routing.
        return False
    if set(fwd) != set(bwd):
        return True
    return any(bwd[lid] != (dst, src) for lid, (src, dst) in fwd.items())


def _edge_map(path: FlowPath) -> dict[str, tuple[str, str]]:
    return {
        edge.link_id: (edge.src_node, edge.dst_node)
        for layer in path.layers
        for edge in layer.edges
    }
