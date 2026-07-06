"""Follow the Rabbit packet-flow tracing.

The service fans out short-lived local tcpdump probes on host-side runtime
interfaces and publishes only link-hit metadata. PCAP bytes never leave the
host and are not exposed through the API.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import secrets
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from dnlab_multinode.models.state import DeploymentState, RuntimeLinkState
from dnlab_multinode.services import state as state_svc
from dnlab_multinode.services.config import parse_topology
from dnlab_multinode.services.flow_reconstruct import (
    LinkRef,
    PacketObservation,
    ReconstructionResult,
    TopoGraph,
    instance_key,
    reconstruct,
)
from dnlab_multinode.services.hosts_config import HostsConfig, load_hosts_config
from dnlab_multinode.services.pcap_parse import IncrementalPcapParser, ParsedPacket, parse_pcap

# Upper bound on packets captured per probe; the flow path needs a handful of
# instances, not a full trace. Keeps the PCAP small and short-lived.
MAX_CAPTURE_PACKETS = 200
CAPTURE_DRAIN_GRACE_SECONDS = 2
REMOTE_CAPTURE_TIMEOUT_SKEW_SECONDS = 2
PROGRESS_THROTTLE_SECONDS = 0.5


class FollowRabbitError(Exception):
    pass


@dataclass(frozen=True)
class FlowFilter:
    src_ip: str
    dst_ip: str
    protocol: str = ""
    src_port: int = 0
    dst_port: int = 0


@dataclass(frozen=True)
class CapturePoint:
    id: str
    link_id: str
    link_type: str
    host: str
    iface: str
    endpoint_a: dict[str, str] = field(default_factory=dict)
    endpoint_b: dict[str, str] = field(default_factory=dict)
    side: str = ""


@dataclass
class FollowRabbitSession:
    session_id: str
    lab_name: str
    source_node: str
    flow: FlowFilter
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    timeout_seconds: int = 60
    hits: dict[str, dict[str, Any]] = field(default_factory=dict)
    observations: list[dict[str, Any]] = field(default_factory=list)
    reconstruction: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    capture_points: int = 0
    probe_count: int = 0
    # Transient capture state used only to build ``reconstruction``; never
    # serialized (raw packet observations and the topology graph stay backend).
    packet_obs: list[PacketObservation] = field(default_factory=list)
    graph: TopoGraph | None = None
    _last_progress_emit: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "lab_name": self.lab_name,
            "source_node": self.source_node,
            "flow": {
                "src_ip": self.flow.src_ip,
                "dst_ip": self.flow.dst_ip,
                "protocol": self.flow.protocol,
                "src_port": self.flow.src_port,
                "dst_port": self.flow.dst_port,
            },
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at or None,
            "timeout_seconds": self.timeout_seconds,
            "hits": list(self.hits.values()),
            "observations": list(self.observations),
            "reconstruction": self.reconstruction,
            "error": self.error,
            "capture_points": self.capture_points,
            "probe_count": self.probe_count,
            "completed_probe_count": len(self.observations),
            "packet_observation_count": len(self.packet_obs),
            **_session_progress_fields(self),
        }


EventCallback = Callable[[dict[str, Any]], None]


def build_bpf(flow: FlowFilter, direction: str = "forward") -> str:
    """Build a directional BPF for one leg of the flow.

    ``forward`` matches packets travelling src -> dst; ``return`` matches the
    reply leg by swapping the IPs and ports. The directional ``src host`` /
    ``dst host`` qualifiers (instead of the symmetric ``host``) ensure a
    reversed packet does not count as a hit for the opposite direction.
    """
    if direction not in {"forward", "return"}:
        raise FollowRabbitError("direction must be forward or return")
    src = _ip(flow.src_ip, "src_ip")
    dst = _ip(flow.dst_ip, "dst_ip")
    protocol = (flow.protocol or "").lower().strip()
    if protocol and protocol not in {"tcp", "udp", "icmp", "icmp6"}:
        raise FollowRabbitError("protocol must be tcp, udp, icmp, or icmp6")
    if flow.src_port:
        _port(flow.src_port, "src_port")
    if flow.dst_port:
        _port(flow.dst_port, "dst_port")
    if (flow.src_port or flow.dst_port) and protocol not in {"tcp", "udp"}:
        raise FollowRabbitError("ports require protocol tcp or udp")

    if direction == "forward":
        a, b, sp, dp = src, dst, flow.src_port, flow.dst_port
    else:
        a, b, sp, dp = dst, src, flow.dst_port, flow.src_port

    parts = [f"src host {a}", f"dst host {b}"]
    if protocol:
        parts.append(protocol)
    if sp:
        parts.append(f"src port {sp}")
    if dp:
        parts.append(f"dst port {dp}")
    return " and ".join(parts)


def build_capture_points(state: DeploymentState) -> list[CapturePoint]:
    points: list[CapturePoint] = []
    for link in state.runtime_links:
        points.extend(_runtime_link_points(link))

    if state.mgmt:
        hosts = sorted({*state.scheduling.keys(), *state.mgmt_anchors.keys(), "master"})
        for host in hosts:
            points.append(CapturePoint(
                id=f"mgmt:{host}:{state.mgmt.bridge}",
                link_id=f"mgmt:{host}",
                link_type="mgmt",
                host=host,
                iface=state.mgmt.bridge,
                endpoint_a={"mgmt": state.mgmt.bridge},
                endpoint_b={"host": host},
            ))
            if state.mgmt.vxlan_iface:
                points.append(CapturePoint(
                    id=f"mgmt-vxlan:{host}:{state.mgmt.vxlan_iface}",
                    link_id=f"mgmt:{host}",
                    link_type="mgmt",
                    host=host,
                    iface=state.mgmt.vxlan_iface,
                    endpoint_a={"mgmt": state.mgmt.vxlan_iface},
                    endpoint_b={"host": host},
                ))

    for rn in state.realnets:
        for host in rn.hosts or ["master"]:
            points.append(CapturePoint(
                id=f"realnet:{rn.name}:{host}:{rn.bridge}",
                link_id=f"realnet:{rn.name}:{host}",
                link_type="real_net",
                host=host,
                iface=rn.bridge,
                endpoint_a={"real_net": rn.name},
                endpoint_b={"host": host},
            ))
    return _dedupe_points(points)


def build_topo_graph(state: DeploymentState) -> TopoGraph:
    """Topology graph for reconstruction, derived from ``state.runtime_links``.

    Each runtime link becomes one edge keyed by its ``RuntimeLinkState.id`` (the
    same id the capture points carry). A real_net endpoint has no VD node, so it
    is represented by a stable pseudo-node ``realnet:<name>``.
    """
    links: list[LinkRef] = []
    for link in state.runtime_links:
        node_a = link.endpoint_a.get("node")
        node_b = link.endpoint_b.get("node")
        if not node_b and link.endpoint_b.get("real_net"):
            node_b = f"realnet:{link.endpoint_b['real_net']}"
        if not node_a and link.endpoint_a.get("real_net"):
            node_a = f"realnet:{link.endpoint_a['real_net']}"
        if node_a and node_b:
            links.append(LinkRef(
                link_id=link.id,
                node_a=node_a,
                node_b=node_b,
                iface_a=link.endpoint_a.get("iface", ""),
                iface_b=link.endpoint_b.get("iface", ""),
            ))
    return TopoGraph(links=links)


class FollowRabbitManager:
    def __init__(self) -> None:
        self._sessions: dict[str, FollowRabbitSession] = {}
        self._tasks: dict[str, list[asyncio.Task]] = {}
        self._lock = asyncio.Lock()

    async def list_sessions(self, lab_name: str | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            sessions = [
                s for s in self._sessions.values()
                if lab_name is None or s.lab_name == lab_name
            ]
            # Refresh the oriented DAG from whatever has been captured so far so
            # a running session already renders the TTL-ordered path live.
            for session in sessions:
                self._reconstruct(session)
            return [s.to_dict() for s in sessions]

    async def start(
        self,
        *,
        topology_file: str,
        hosts_file: str | None,
        source_node: str,
        flow: FlowFilter,
        timeout_seconds: int,
        emit: EventCallback,
    ) -> dict[str, Any]:
        bpf_by_direction = {
            "forward": build_bpf(flow, "forward"),
            "return": build_bpf(flow, "return"),
        }
        cfg = load_hosts_config(hosts_file)
        topo = parse_topology(topology_file, hosts_file=hosts_file)
        if source_node not in topo.nodes:
            raise FollowRabbitError(f"source node {source_node!r} not found")
        state = state_svc.load_state(topo.name, Path(topology_file).parent)
        if state is None:
            raise FollowRabbitError(f"lab {topo.name!r} is not deployed")
        max_sessions = cfg.follow_the_rabbit.max_sessions

        async with self._lock:
            active = sum(1 for s in self._sessions.values() if s.status == "running")
            if active >= max_sessions:
                raise FollowRabbitError("too many active Follow the Rabbit sessions")
            session = FollowRabbitSession(
                session_id=secrets.token_hex(8),
                lab_name=topo.name,
                source_node=source_node,
                flow=flow,
                timeout_seconds=_timeout(timeout_seconds),
            )
            self._sessions[session.session_id] = session

        graph = build_topo_graph(state)
        session.graph = graph
        # BFS from the source anchor → only capture on links that can carry the
        # flow. If the graph is unavailable, fall back to every capture point.
        candidate = set(graph.candidate_links(source_node))
        points = [
            p for p in build_capture_points(state)
            if not candidate or p.link_id in candidate
        ]
        session.capture_points = len(points)
        session.probe_count = len(points) * len(bpf_by_direction)
        emit(_event("session_started", session, _session_summary(session)))
        # One probe per direction per capture point: a reversed packet only
        # matches its own directional BPF, so forward and return are separated.
        tasks = [
            asyncio.create_task(self._watch_point(session, point, cfg, bpf, direction, emit))
            for point in points
            for direction, bpf in bpf_by_direction.items()
        ]
        tasks.append(asyncio.create_task(self._finish_after_timeout(session, emit)))
        async with self._lock:
            self._tasks[session.session_id] = tasks
        return session.to_dict()

    async def stop(self, session_id: str, emit: EventCallback | None = None) -> dict[str, Any]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise FollowRabbitError("session not found")
            session.status = "stopped"
            session.completed_at = time.time()
            tasks = self._tasks.pop(session_id, [])
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reconstruct(session)
        if emit is not None:
            emit(_event("session_done", session, _session_done_payload(session)))
        return session.to_dict()

    async def _watch_point(
        self,
        session: FollowRabbitSession,
        point: CapturePoint,
        cfg: HostsConfig,
        bpf: str,
        direction: str,
        emit: EventCallback,
    ) -> None:
        packets: list[PacketObservation] = []

        async def on_packet(pkt: ParsedPacket) -> None:
            packet = _observation_from_packet(pkt, point, direction)
            packets.append(packet)
            async with self._lock:
                session.packet_obs.append(packet)
            emit_hit = await self._record_hit(session, point, direction, [f"{len(packets)} packet(s)"])
            if emit_hit is not None:
                emit(_event("link_hit", session, emit_hit))
                await self._emit_session_progress(session, emit, force=True)
            else:
                await self._emit_session_progress(session, emit)

        try:
            rc, err = await _run_tcpdump_stream(
                point,
                cfg,
                bpf,
                _remote_capture_timeout(session.timeout_seconds),
                on_packet,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            observation = _observation(point, rc=1, error=str(exc), direction=direction)
            async with self._lock:
                session.observations.append(observation)
            emit(_event("session_error", session, observation))
            return
        observation = _observation(
            point,
            rc=rc,
            sample=[f"{len(packets)} packet(s)"] if packets else [],
            error=err if rc and not packets else "",
            direction=direction,
            packet_count=len(packets),
        )
        async with self._lock:
            session.observations.append(observation)
        await self._emit_session_progress(session, emit, force=True)
        if not packets and rc not in {124, 143} and err:
            emit(_event("session_error", session, observation))

    async def _record_hit(
        self,
        session: FollowRabbitSession,
        point: CapturePoint,
        direction: str,
        sample: list[str],
    ) -> dict[str, Any] | None:
        hit_key = f"{direction}:{point.link_id}"
        hit = {
            "link_id": point.link_id,
            "link_type": point.link_type,
            "direction": direction,
            "host": point.host,
            "iface": point.iface,
            "endpoint_a": point.endpoint_a,
            "endpoint_b": point.endpoint_b,
            "side": point.side,
            "sample": sample,
            "observed_at": time.time(),
        }
        async with self._lock:
            if session.status == "running" and hit_key not in session.hits:
                session.hits[hit_key] = hit
                return hit
        return None

    async def _emit_session_progress(
        self,
        session: FollowRabbitSession,
        emit: EventCallback,
        *,
        force: bool = False,
    ) -> None:
        now = time.time()
        if not force and now - session._last_progress_emit < PROGRESS_THROTTLE_SECONDS:
            return
        session._last_progress_emit = now
        self._reconstruct(session)
        emit(_event("session_progress", session, _session_progress_payload(session)))

    async def _finish_after_timeout(self, session: FollowRabbitSession, emit: EventCallback) -> None:
        deadline = asyncio.get_running_loop().time() + session.timeout_seconds
        await asyncio.sleep(0)
        async with self._lock:
            if session.status != "running":
                return
            tasks = list(self._tasks.get(session.session_id, []))
        others = [task for task in tasks if task is not asyncio.current_task()]
        if others:
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            done, pending = await asyncio.wait(
                others,
                timeout=remaining,
                return_when=asyncio.ALL_COMPLETED,
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            if pending:
                done, pending = await asyncio.wait(
                    pending,
                    timeout=CAPTURE_DRAIN_GRACE_SECONDS,
                    return_when=asyncio.ALL_COMPLETED,
                )
                if done:
                    await asyncio.gather(*done, return_exceptions=True)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        else:
            try:
                await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
            except asyncio.CancelledError:
                raise
        async with self._lock:
            if session.status != "running":
                return
            session.status = "done"
            session.completed_at = time.time()
            self._tasks.pop(session.session_id, None)
        self._reconstruct(session)
        emit(_event("session_done", session, _session_done_payload(session)))

    def _reconstruct(self, session: FollowRabbitSession) -> None:
        """Build the oriented TTL DAG from the captured observations (pure core)."""
        graph = session.graph
        if graph is None:
            return
        known = {link.link_id for link in graph.links}
        observations = _dedupe_observations(
            [o for o in session.packet_obs if o.link_id in known]
        )
        result = reconstruct(graph, observations, session.source_node)
        session.reconstruction = _serialize_result(result, graph, observations)


def _runtime_link_points(link: RuntimeLinkState) -> list[CapturePoint]:
    points = []
    for side, host, iface in [
        ("a", link.host_a, link.host_endpoint_a),
        ("b", link.host_b, link.host_endpoint_b),
    ]:
        if host and iface:
            points.append(CapturePoint(
                id=f"{link.id}:{side}:{host}:{iface}",
                link_id=link.id,
                link_type=link.link_type,
                host=host,
                iface=iface,
                endpoint_a=dict(link.endpoint_a),
                endpoint_b=dict(link.endpoint_b),
                side=side,
            ))
    return points


def _dedupe_points(points: list[CapturePoint]) -> list[CapturePoint]:
    seen: set[tuple[str, str, str]] = set()
    out: list[CapturePoint] = []
    for point in points:
        key = (point.host, point.iface, point.link_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(point)
    return out


async def _run_tcpdump(
    point: CapturePoint,
    cfg: HostsConfig,
    bpf: str,
    timeout_seconds: int,
) -> tuple[int, bytes, str]:
    """Capture up to ``MAX_CAPTURE_PACKETS`` matching frames as raw PCAP.

    ``-w -`` streams binary PCAP on stdout (parsed backend-side, never exposed);
    ``-U`` packet-buffers it so a timeout still flushes what was seen. stderr
    carries tcpdump's textual diagnostics only.
    """
    host = cfg.all_hosts.get(point.host)
    if host is None:
        return 127, b"", f"unknown host {point.host!r}"
    remote = (
        f"timeout {int(timeout_seconds)}s tcpdump -w - -U -n "
        f"-i {shlex.quote(point.iface)} -c {MAX_CAPTURE_PACKETS} {shlex.quote(bpf)}"
    )
    proc = await asyncio.create_subprocess_exec(
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-i", host.ssh_key,
        f"{host.ssh_user}@{host.host}",
        remote,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await proc.communicate()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        raise
    return (
        proc.returncode or 0,
        out_b,
        err_b.decode(errors="replace").strip(),
    )


async def _run_tcpdump_stream(
    point: CapturePoint,
    cfg: HostsConfig,
    bpf: str,
    timeout_seconds: int,
    on_packet: Callable[[ParsedPacket], Any],
) -> tuple[int, str]:
    """Stream tcpdump stdout and call ``on_packet`` as full PCAP records arrive."""
    host = cfg.all_hosts.get(point.host)
    if host is None:
        return 127, f"unknown host {point.host!r}"
    remote = (
        f"timeout {int(timeout_seconds)}s tcpdump -w - -U -n "
        f"-i {shlex.quote(point.iface)} -c {MAX_CAPTURE_PACKETS} {shlex.quote(bpf)}"
    )
    proc = await asyncio.create_subprocess_exec(
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-i", host.ssh_key,
        f"{host.ssh_user}@{host.host}",
        remote,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    parser = IncrementalPcapParser()
    err_task = asyncio.create_task(proc.stderr.read()) if proc.stderr else None
    try:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            for pkt in parser.feed(chunk):
                maybe = on_packet(pkt)
                if asyncio.iscoroutine(maybe):
                    await maybe
        rc = await proc.wait()
        err_b = await err_task if err_task else b""
        return rc or 0, err_b.decode(errors="replace").strip()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        if err_task:
            err_task.cancel()
            with contextlib.suppress(BaseException):
                await err_task
        raise


def _observations_from_pcap(
    pcap_bytes: bytes,
    point: CapturePoint,
    direction: str,
) -> list[PacketObservation]:
    """Parse captured PCAP into per-packet observations for the pure core."""
    observations: list[PacketObservation] = []
    for pkt in parse_pcap(pcap_bytes):
        observations.append(_observation_from_packet(pkt, point, direction))
    return observations


def _observation_from_packet(
    pkt: ParsedPacket,
    point: CapturePoint,
    direction: str,
) -> PacketObservation:
    key = instance_key(
        pkt.proto,
        icmp_id=pkt.icmp_id,
        icmp_seq=pkt.icmp_seq,
        tcp_seq=pkt.tcp_seq,
        payload_len=pkt.payload_len,
        ip_id=pkt.ip_id,
    )
    return PacketObservation(
        link_id=point.link_id,
        ttl=pkt.ttl,
        ts_local=pkt.ts,
        host=point.host,
        direction=direction,
        instance_key=key,
        five_tuple=(pkt.src_ip, pkt.dst_ip, pkt.proto, pkt.src_port, pkt.dst_port),
    )


def _dedupe_observations(observations: list[PacketObservation]) -> list[PacketObservation]:
    """Collapse the same instance seen twice on one link (both endpoints probed)."""
    seen: set[tuple[str, str, str]] = set()
    out: list[PacketObservation] = []
    for o in observations:
        if o.instance_key is None:
            out.append(o)
            continue
        key = (o.link_id, o.direction, o.instance_key)
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out


def _serialize_result(
    result: ReconstructionResult,
    graph: TopoGraph | None = None,
    observations: list[PacketObservation] | None = None,
) -> dict[str, Any]:
    link_meta = _link_metadata(graph)
    last_packet_at = _last_packet_at(observations or [])
    return {
        "forward": _serialize_path(result.forward, link_meta, last_packet_at),
        "backward": _serialize_path(result.backward, link_meta, last_packet_at),
        "asymmetric": result.asymmetric,
    }


def _serialize_path(
    path,
    link_meta: dict[str, dict[str, Any]] | None = None,
    last_packet_at: dict[tuple[str, str], float] | None = None,
) -> dict[str, Any]:
    link_meta = link_meta or {}
    last_packet_at = last_packet_at or {}
    return {
        "leg": path.leg,
        "classification": path.classification,
        "layers": [
            {
                "ttl": layer.ttl,
                "state": layer.state,
                "edges": [
                    {
                        "link_id": e.link_id,
                        "src_node": e.src_node,
                        "dst_node": e.dst_node,
                        **_oriented_link_metadata(
                            link_meta.get(e.link_id, {}),
                            e.src_node,
                            e.dst_node,
                        ),
                        "last_packet_at": last_packet_at.get((e.link_id, path.leg)),
                        "ttl": e.ttl,
                        "weight": e.weight,
                        "weight_quality": e.weight_quality,
                    }
                    for e in layer.edges
                ],
            }
            for layer in path.layers
        ],
        "segments": [
            {
                "ttl_high": s.ttl_high,
                "ttl_low": s.ttl_low,
                "state": s.state,
                "missing_ttl": s.missing_ttl,
            }
            for s in path.segments
        ],
    }


def _link_metadata(graph: TopoGraph | None) -> dict[str, dict[str, Any]]:
    if graph is None:
        return {}
    return {
        link.link_id: {
            "node_a": link.node_a,
            "node_b": link.node_b,
            "iface_a": link.iface_a,
            "iface_b": link.iface_b,
        }
        for link in graph.links
    }


def _oriented_link_metadata(meta: dict[str, Any], src_node: str, dst_node: str) -> dict[str, Any]:
    node_a = meta.get("node_a", "")
    node_b = meta.get("node_b", "")
    iface_a = meta.get("iface_a", "")
    iface_b = meta.get("iface_b", "")
    if src_node == node_a and dst_node == node_b:
        src_iface, dst_iface = iface_a, iface_b
    elif src_node == node_b and dst_node == node_a:
        src_iface, dst_iface = iface_b, iface_a
    else:
        src_iface, dst_iface = "", ""
    return {
        "source_iface": src_iface,
        "target_iface": dst_iface,
        "endpoint_a": {"node": node_a, "iface": iface_a} if node_a else {},
        "endpoint_b": {"node": node_b, "iface": iface_b} if node_b else {},
    }


def _last_packet_at(observations: list[PacketObservation]) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for obs in observations:
        key = (obs.link_id, obs.direction)
        out[key] = max(out.get(key, 0), obs.ts_local)
    return out


def _event(kind: str, session: FollowRabbitSession, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "phase": "follow-rabbit",
        "status": kind,
        "event": kind,
        "session_id": session.session_id,
        "lab_name": session.lab_name,
        "detail": kind.replace("_", " "),
        "data": data or {},
        "ts": time.time(),
    }


def _session_summary(session: FollowRabbitSession) -> dict[str, Any]:
    return {
        "capture_points": session.capture_points,
        "probe_count": session.probe_count,
        "completed_probe_count": len(session.observations),
        "packet_observation_count": len(session.packet_obs),
        "completed_at": session.completed_at or None,
        **_session_progress_fields(session),
    }


def _session_progress_payload(session: FollowRabbitSession) -> dict[str, Any]:
    return {
        **_session_summary(session),
        "reconstruction": session.reconstruction,
    }


def _session_done_payload(session: FollowRabbitSession) -> dict[str, Any]:
    return {
        **_session_summary(session),
        "reconstruction": session.reconstruction,
    }


def _session_progress_fields(session: FollowRabbitSession) -> dict[str, Any]:
    now = session.completed_at or time.time()
    elapsed = max(0.0, now - session.started_at)
    remaining = 0.0 if session.status != "running" else max(0.0, session.timeout_seconds - elapsed)
    completed = len(session.observations)
    active = max(0, session.probe_count - completed) if session.status == "running" else 0
    if session.status == "running" and session.timeout_seconds:
        progress = min(99.0, (elapsed / session.timeout_seconds) * 100)
    else:
        progress = 100.0
    return {
        "active_probe_count": active,
        "elapsed_seconds": round(elapsed, 1),
        "remaining_seconds": round(remaining, 1),
        "progress_percent": round(progress, 1),
    }


def _observation(
    point: CapturePoint,
    *,
    rc: int,
    sample: list[str] | None = None,
    error: str = "",
    direction: str = "forward",
    packet_count: int = 0,
) -> dict[str, Any]:
    return {
        "capture_point": point.id,
        "link_id": point.link_id,
        "link_type": point.link_type,
        "direction": direction,
        "host": point.host,
        "iface": point.iface,
        "side": point.side,
        "rc": rc,
        "packet_count": packet_count,
        "sample": sample or [],
        "error": error,
        "observed_at": time.time(),
    }


def _ip(value: str, field: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise FollowRabbitError(f"{field} must be a valid IP address") from exc


def _port(value: int, field: str) -> None:
    if not (1 <= int(value) <= 65535):
        raise FollowRabbitError(f"{field} must be between 1 and 65535")


def _timeout(value: int) -> int:
    if not value:
        return 60
    return max(5, min(int(value), 600))


def _remote_capture_timeout(session_timeout: int) -> int:
    return max(1, int(session_timeout) - REMOTE_CAPTURE_TIMEOUT_SKEW_SECONDS)


follow_rabbit_manager = FollowRabbitManager()
