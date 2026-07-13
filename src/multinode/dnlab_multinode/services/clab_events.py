"""Best-effort Containerlab events bridge for per-host runtime labs."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from dnlab_multinode.models.state import DeploymentState
from dnlab_multinode.services import state as state_svc
from dnlab_multinode.services.clab_capabilities import PER_HOST_APPLY
from dnlab_multinode.services.config import parse_topology
from dnlab_multinode.services.ssh import SSHClient

log = logging.getLogger(__name__)

EventPublisher = Callable[[dict[str, Any]], None]
DEFAULT_WINDOW_SECONDS = 65
WINDOW_SECONDS_ENV = "DNLAB_CLAB_EVENTS_WINDOW_SECONDS"
DEFAULT_RECONNECT_DELAY_SECONDS = 3.0
RECONNECT_DELAY_SECONDS_ENV = "DNLAB_CLAB_EVENTS_RECONNECT_DELAY_SECONDS"


class ContainerlabEventsError(Exception):
    pass


@dataclass
class EventsWatchHandle:
    key: str
    lab_name: str
    topic: str
    hosts: list[str]
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "lab_name": self.lab_name,
            "topic": self.topic,
            "hosts": list(self.hosts),
            "started_at": self.started_at,
        }


class ContainerlabEventsManager:
    """Manage bounded, reconnecting event streams for deployed labs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._watchers: dict[str, _LabEventsWatcher] = {}

    def start(
        self,
        *,
        topology_file: str,
        hosts_file: str | None,
        topic: str,
        publish: EventPublisher,
        window_seconds: int | None = None,
    ) -> EventsWatchHandle:
        topo = parse_topology(topology_file, hosts_file=hosts_file)
        state = state_svc.load_state(topo.name, Path(topology_file).parent)
        if state is None:
            raise ContainerlabEventsError(f"Lab '{topo.name}' is not deployed")
        if state.runtime_mode != PER_HOST_APPLY:
            raise ContainerlabEventsError(
                f"Lab '{topo.name}' uses runtime mode {state.runtime_mode!r}; "
                "Containerlab events watch requires per-host-apply"
            )

        topology_by_host = _topology_by_host(state)
        if not topology_by_host:
            raise ContainerlabEventsError(
                f"Lab '{topo.name}' has no per-host topology files in state"
            )

        key = _watch_key(topo.name, topic)
        with self._lock:
            current = self._watchers.get(key)
            if current is not None and current.running:
                return current.handle
            watcher = _LabEventsWatcher(
                key=key,
                lab_name=topo.name,
                topic=topic,
                topology_by_host=topology_by_host,
                hosts=topo.all_hosts,
                state=state,
                publish=publish,
                window_seconds=window_seconds or _event_window_seconds(),
                reconnect_delay_seconds=_event_reconnect_delay_seconds(),
            )
            self._watchers[key] = watcher
            watcher.start()
            return watcher.handle

    def stop(self, *, lab_name: str, topic: str) -> bool:
        key = _watch_key(lab_name, topic)
        with self._lock:
            watcher = self._watchers.pop(key, None)
        if watcher is None:
            return False
        watcher.stop()
        return True

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                watcher.handle.to_dict()
                for watcher in self._watchers.values()
                if watcher.running
            ]


class _LabEventsWatcher:
    def __init__(
        self,
        *,
        key: str,
        lab_name: str,
        topic: str,
        topology_by_host: dict[str, str],
        hosts: dict[str, Any],
        state: DeploymentState,
        publish: EventPublisher,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        reconnect_delay_seconds: float = DEFAULT_RECONNECT_DELAY_SECONDS,
    ) -> None:
        self.key = key
        self.lab_name = lab_name
        self.topic = topic
        self.topology_by_host = topology_by_host
        self.hosts = hosts
        self.publish = publish
        self.window_seconds = max(5, int(window_seconds))
        self.reconnect_delay_seconds = max(0.0, float(reconnect_delay_seconds))
        self.stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._seen: set[str] = set()
        self._seen_order: list[str] = []
        self._container_to_node = {
            runtime.container: node
            for node, runtime in (state.node_runtime or {}).items()
            if runtime.container
        }
        self._known_nodes = set(state.node_runtime or {})
        self.handle = EventsWatchHandle(
            key=key,
            lab_name=lab_name,
            topic=topic,
            hosts=sorted(topology_by_host),
        )

    @property
    def running(self) -> bool:
        return not self.stop_event.is_set()

    def start(self) -> None:
        for host_name, topology_file in sorted(self.topology_by_host.items()):
            host = self.hosts.get(host_name)
            if host is None:
                continue
            thread = threading.Thread(
                target=self._run_host,
                args=(host_name, host, topology_file),
                name=f"clab-events-{self.lab_name}-{host_name}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()
        self._publish(
            host="",
            status="started",
            detail=f"watching Containerlab events on {len(self._threads)} host(s)",
            data={"hosts": sorted(self.topology_by_host)},
        )

    def stop(self) -> None:
        self.stop_event.set()

    def _run_host(self, host_name: str, host: Any, topology_file: str) -> None:
        while not self.stop_event.is_set():
            client = SSHClient(
                host=host.host,
                user=host.ssh_user,
                key_path=host.ssh_key,
                name=host_name,
            )
            try:
                client.connect()
                self._publish(
                    host=host_name,
                    status="started",
                    detail="Containerlab event stream connected",
                    data={"topology_file": topology_file},
                )
                self._resync_host(client, host_name, topology_file)
                rc, err = client.stream_clab_events(
                    topology_file,
                    stop_event=self.stop_event,
                    on_line=lambda line: self._handle_line(host_name, line),
                    window_seconds=self.window_seconds,
                )
                if not self.stop_event.is_set() and rc not in {0, 124}:
                    self._publish(
                        host=host_name,
                        status="warn",
                        detail=f"Containerlab event stream exited rc={rc}",
                        data={"stderr": err[-500:] if err else ""},
                    )
            except Exception as exc:
                if not self.stop_event.is_set():
                    self._publish(
                        host=host_name,
                        status="warn",
                        detail=f"Containerlab event stream failed: {exc}",
                    )
            finally:
                client.close()
            if not self.stop_event.is_set():
                self.stop_event.wait(self.reconnect_delay_seconds)

    def _resync_host(self, client: SSHClient, host_name: str, topology_file: str) -> None:
        try:
            inspect_raw = client.inspect_clab(topology_file)
            interfaces_raw = client.inspect_clab_interfaces(topology_file)
            inspect_data = json.loads(inspect_raw) if inspect_raw else {}
            interfaces_data = json.loads(interfaces_raw) if interfaces_raw else {}
            nodes = _inspect_nodes(inspect_data, self._known_nodes, self._container_to_node)
            interfaces_by_node = _interface_counts(
                interfaces_data,
                self._known_nodes,
                self._container_to_node,
            )
            self._publish(
                host=host_name,
                status="ok",
                detail=(
                    f"resync: {len(nodes)} node(s), "
                    f"{sum(interfaces_by_node.values())} interface(s)"
                ),
                data={
                    "event": "resync",
                    "topology_file": topology_file,
                    "nodes": sorted(nodes),
                    "interfaces_by_node": interfaces_by_node,
                },
            )
        except Exception as exc:
            self._publish(
                host=host_name,
                status="warn",
                detail=f"Containerlab resync failed: {exc}",
                data={"event": "resync"},
            )

    def _handle_line(self, host_name: str, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        for record in _event_records(payload):
            event = normalise_event(
                record,
                lab_name=self.lab_name,
                host=host_name,
                container_to_node=self._container_to_node,
            )
            if event is None:
                continue
            signature = json.dumps(
                {
                    "host": event.get("host"),
                    "node": event.get("data", {}).get("node"),
                    "container": event.get("data", {}).get("container"),
                    "interface": event.get("data", {}).get("interface"),
                    "event": event.get("data", {}).get("event"),
                    "state": event.get("data", {}).get("state"),
                },
                sort_keys=True,
            )
            if signature in self._seen:
                continue
            self._remember(signature)
            self._publish(**event)

    def _remember(self, signature: str) -> None:
        self._seen.add(signature)
        self._seen_order.append(signature)
        while len(self._seen_order) > 1000:
            old = self._seen_order.pop(0)
            self._seen.discard(old)

    def _publish(
        self,
        *,
        host: str,
        status: str,
        detail: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.publish({
            "phase": "clab-events",
            "status": status,
            "host": host,
            "detail": detail,
            "elapsed_ms": 0,
            "data": {
                "source": "containerlab-events",
                "lab_name": self.lab_name,
                **(data or {}),
            },
        })


def normalise_event(
    record: dict[str, Any],
    *,
    lab_name: str,
    host: str,
    container_to_node: dict[str, str],
) -> dict[str, Any] | None:
    """Convert one Containerlab JSON event into the dNLab progress shape."""
    if not isinstance(record, dict):
        return None

    container = _first_str(
        record,
        "container",
        "container_name",
        "containerName",
        "Container",
        "ContainerName",
        "name",
        "Name",
    )
    labels = _labels(record)
    event_lab = _label_value(labels, "clab-lab-name", "containerlab", "lab")
    node = _first_str(record, "node", "node_name", "nodeName", "Node", "NodeName")
    if not node:
        node = _label_value(labels, "clab-node-name", "node")
    if not node and container:
        node = container_to_node.get(container, "")
    interface = _first_str(
        record,
        "interface",
        "interface_name",
        "interfaceName",
        "ifname",
        "IfName",
        "Interface",
    )
    peer = _first_str(record, "peer", "peer_name", "peerName", "Peer")
    event_name = _first_str(
        record,
        "event",
        "Event",
        "action",
        "Action",
        "type",
        "Type",
        "status",
        "Status",
        "state",
        "State",
    )
    state = _first_str(record, "state", "State", "status", "Status")

    if not any([container, node, interface, event_name, state]):
        return None

    raw_status = (event_name or state or "event").lower()
    status = _event_status(raw_status)
    subject = node or container or interface or "runtime"
    details = [subject]
    if event_name:
        details.append(event_name)
    if state and state != event_name:
        details.append(state)
    if interface:
        details.append(interface)
    if peer:
        details.append(f"peer={peer}")

    data = {
        "event": event_name,
        "state": state,
        "node": node,
        "container": container,
        "interface": interface,
        "peer": peer,
        "event_lab": event_lab,
        "labels": labels,
        "raw": record,
    }
    return {
        "host": host,
        "status": status,
        "detail": " ".join(part for part in details if part),
        "data": data,
    }


def _topology_by_host(state: DeploymentState) -> dict[str, str]:
    result: dict[str, str] = {}
    for runtime in (state.node_runtime or {}).values():
        if runtime.host and runtime.topology_file:
            result.setdefault(runtime.host, runtime.topology_file)
    return result


def _watch_key(lab_name: str, topic: str) -> str:
    return f"{topic}:{lab_name}"


def _event_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("events", "items", "interfaces", "containers"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _inspect_nodes(
    payload: Any,
    known_nodes: set[str],
    container_to_node: dict[str, str],
) -> set[str]:
    nodes: set[str] = set()
    for record in _event_records(payload):
        labels = _labels(record)
        node = (
            _label_value(labels, "clab-node-name", "node")
            or _first_str(record, "node", "node_name", "nodeName", "name", "Name")
        )
        container = _first_str(
            record,
            "container",
            "container_name",
            "containerName",
            "name",
            "Name",
        )
        resolved = _resolve_node(node, known_nodes, container_to_node)
        if not resolved:
            resolved = _resolve_node(container, known_nodes, container_to_node)
        if resolved:
            nodes.add(resolved)
    if not nodes and isinstance(payload, dict):
        for key in payload:
            resolved = _resolve_node(str(key), known_nodes, container_to_node)
            if resolved:
                nodes.add(resolved)
    return nodes


def _interface_counts(
    payload: Any,
    known_nodes: set[str],
    container_to_node: dict[str, str],
) -> dict[str, int]:
    counts: dict[str, int] = {}

    def walk(obj: Any, node_hint: str = "") -> None:
        if isinstance(obj, list):
            for item in obj:
                walk(item, node_hint)
            return
        if not isinstance(obj, dict):
            return

        next_hint = _resolve_node(node_hint, known_nodes, container_to_node)
        labels = _labels(obj)
        node = _resolve_node(
            _label_value(labels, "clab-node-name", "node")
            or _first_str(
                obj,
                "node",
                "node_name",
                "nodeName",
                "container",
                "container_name",
                "containerName",
            ),
            known_nodes,
            container_to_node,
        ) or next_hint
        iface = _first_str(
            obj,
            "interface",
            "interface_name",
            "interfaceName",
            "ifname",
            "name",
        )
        if node and iface:
            counts[node] = counts.get(node, 0) + 1
            return

        for key, value in obj.items():
            child_hint = _resolve_node(str(key), known_nodes, container_to_node) or next_hint
            walk(value, child_hint)

    walk(payload)
    return dict(sorted(counts.items()))


def _resolve_node(
    value: str,
    known_nodes: set[str],
    container_to_node: dict[str, str],
) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value in known_nodes:
        return value
    if value in container_to_node:
        return container_to_node[value]
    for node in known_nodes:
        if value.endswith(f"-{node}"):
            return node
    return ""


def _labels(record: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for key in ("labels", "Labels", "docker_labels", "dockerLabels"):
        value = record.get(key)
        if isinstance(value, dict):
            labels.update({
                str(label_key): str(label_value)
                for label_key, label_value in value.items()
                if label_value is not None
            })
    for key, value in record.items():
        if value is None:
            continue
        for prefix in ("label.", "Label."):
            if str(key).startswith(prefix):
                labels[str(key)[len(prefix):]] = str(value)
    return labels


def _label_value(labels: dict[str, str], *names: str) -> str:
    for name in names:
        value = labels.get(name)
        if value:
            return value
    return ""


def _first_str(record: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _event_status(value: str) -> str:
    value = value.lower()
    if value in {"die", "destroy", "remove", "removed", "disconnect", "down", "exited"}:
        return "warn"
    if value in {"error", "failed", "failure"}:
        return "error"
    if value in {"running", "start", "started", "connect", "connected", "up", "create", "created"}:
        return "ok"
    return "progress"


def _event_window_seconds() -> int:
    try:
        return max(5, int(os.getenv(WINDOW_SECONDS_ENV, str(DEFAULT_WINDOW_SECONDS))))
    except ValueError:
        return DEFAULT_WINDOW_SECONDS


def _event_reconnect_delay_seconds() -> float:
    try:
        return max(
            0.0,
            float(
                os.getenv(
                    RECONNECT_DELAY_SECONDS_ENV,
                    str(DEFAULT_RECONNECT_DELAY_SECONDS),
                )
            ),
        )
    except ValueError:
        return DEFAULT_RECONNECT_DELAY_SECONDS


manager = ContainerlabEventsManager()
