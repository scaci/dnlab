"""Conservative cleanup reconciler for stale dNLab lab artifacts."""

from __future__ import annotations

import json
import logging
import re
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dnlab_multinode.models.state import DeploymentState
from dnlab_multinode.services import state as state_svc
from dnlab_multinode.services.hosts_config import HostsConfig
from dnlab_multinode.services.paths import PATHS
from dnlab_multinode.services.ssh import SSHClient, create_clients
from dnlab_multinode.utils import naming

log = logging.getLogger(__name__)

DEFAULT_STATE_FILE = Path(PATHS.lab_cleanup_state)
_DOCKER_STATES_RUNNING = {"running"}
_CLEANABLE_CONTAINER_STATES = {"created", "exited", "dead", "removing"}
_SHARED_NETWORKS = {"dnlab-jumphost", "dnlab-realnet"}
_MGMT_ANCHOR_RE = re.compile(r"^clab-dnlab-(?P<lab>.+?)-mgmt-.+-mgmt-anchor$")
_SERVICE_CONTAINER_RE = re.compile(
    r"^dnlab-(?P<lab>.+?)-(?:dns|jumphost|runtime-relay|syslog|log-shipper)$"
)
_REALNET_CONTAINER_RE = re.compile(r"^dnlab-(?P<lab>.+?)-[^-]+-realnet$")
_LEGACY_CLAB_CONTAINER_RE = re.compile(r"^clab-(?P<lab>.+?)-[^-]+$")


@dataclass
class CleanupArtifact:
    kind: str
    name: str
    host: str
    lab: str
    state: str = ""
    age_seconds: int | None = None
    shared: bool = False
    source: str = "live"
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "host": self.host,
            "lab": self.lab,
            "state": self.state,
            "age_seconds": self.age_seconds,
            "shared": self.shared,
            "source": self.source,
            "metadata": self.metadata,
        }


@dataclass
class HostCleanupInventory:
    name: str
    host: str
    reachable: bool = False
    error: str = ""
    artifacts: list[CleanupArtifact] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "host": self.host,
            "reachable": self.reachable,
            "error": self.error,
            "artifacts": [a.to_dict() for a in self.artifacts],
        }


@dataclass
class CleanupAction:
    action: str
    artifact: CleanupArtifact
    command: str
    reason: str
    executed: bool = False
    ok: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "artifact": self.artifact.to_dict(),
            "command": self.command,
            "reason": self.reason,
            "executed": self.executed,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass
class LabCleanupPlan:
    lab: str
    protected: bool = False
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    actions: list[CleanupAction] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "lab": self.lab,
            "protected": self.protected,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "actions": [a.to_dict() for a in self.actions],
        }


@dataclass
class CleanupReport:
    updated_at: str
    dry_run: bool
    grace_seconds: int
    scanned_hosts: dict[str, HostCleanupInventory] = field(default_factory=dict)
    labs: dict[str, LabCleanupPlan] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    reconcile_count: int = 0
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "updated_at": self.updated_at,
            "dry_run": self.dry_run,
            "grace_seconds": self.grace_seconds,
            "reconcile_count": self.reconcile_count,
            "duration_ms": self.duration_ms,
            "hosts": {n: h.to_dict() for n, h in self.scanned_hosts.items()},
            "labs": {n: p.to_dict() for n, p in self.labs.items()},
            "errors": self.errors,
        }


def discover_state_files(topologies_dir: Path | str = PATHS.topologies_dir) -> dict[str, DeploymentState]:
    """Load all deployment state files from the configured topologies dir."""
    states: dict[str, DeploymentState] = {}
    root = Path(topologies_dir)
    if not root.exists():
        return states
    for path in sorted(root.glob(".*.multinode.json")):
        lab = path.name[1:-len(".multinode.json")]
        state = state_svc.load_state(lab, root)
        if state is not None:
            states[lab] = state
    return states


def parse_artifact_lab(kind: str, name: str, known_labs: set[str] | None = None) -> str:
    """Return the lab name encoded in a dNLab artifact name, or ``""``."""
    known_labs = known_labs or set()
    if name in _SHARED_NETWORKS:
        return ""
    for lab in sorted(known_labs, key=len, reverse=True):
        if kind == "network" and name in {lab, f"mgmt-{lab}", naming.mgmt_network_name(lab)}:
            return lab
        prefixes = (
            f"dnlab-{lab}-",
            f"clab-dnlab-{lab}-",
            f"clab-{lab}-",
        )
        if any(name.startswith(p) for p in prefixes):
            return lab

    if name.startswith("clab-dnlab-"):
        lab = _parse_micro_container_lab(name)
        if lab:
            return lab

    for pattern in (
        _MGMT_ANCHOR_RE,
        _SERVICE_CONTAINER_RE,
        _REALNET_CONTAINER_RE,
        _LEGACY_CLAB_CONTAINER_RE,
    ):
        match = pattern.match(name)
        if match:
            return match.group("lab")

    if kind == "interface":
        match = re.match(r"^(?:vx-|br-|vrf-)(?P<lab>[A-Za-z0-9_.-]+)", name)
        if match:
            return match.group("lab").removesuffix("-mgmt")
    return ""


def _parse_micro_container_lab(name: str) -> str:
    """Parse ``clab-dnlab-<lab>-<vd>-<vd>`` without state.

    Per-VD micro topologies repeat the VD name twice: once in the
    topology/project name and once as the node name. Use that repetition to
    avoid treating ``dnlab-<lab>-<vd>`` as the lab name.
    """
    body = name.removeprefix("clab-dnlab-")
    parts = body.split("-")
    for i in range(1, len(parts)):
        tail = parts[i:]
        if not tail:
            continue
        half = len(tail) // 2
        if len(tail) % 2 == 0 and tail[:half] == tail[half:]:
            return "-".join(parts[:i])
    return ""


def expected_artifacts_from_state(state: DeploymentState) -> list[CleanupArtifact]:
    """Build state-derived cleanup targets that are safe to name exactly."""
    lab = state.lab_name
    out: list[CleanupArtifact] = []
    for runtime in state.node_runtime.values():
        if runtime.host and runtime.container:
            out.append(CleanupArtifact("container", runtime.container, runtime.host, lab, source="state"))
    if state.dns:
        out.append(CleanupArtifact("container", state.dns.container, state.dns.node, lab, source="state"))
    if state.jumphost:
        out.append(CleanupArtifact("container", state.jumphost.container, state.jumphost.node, lab, source="state"))
    for host, relay in state.runtime_relays.items():
        out.append(CleanupArtifact("container", relay.container, host, lab, source="state"))
    for anchor in state.mgmt_anchors.values():
        if anchor.host and anchor.container:
            out.append(CleanupArtifact(
                "container",
                anchor.container,
                anchor.host,
                lab,
                source="state",
                metadata={"role": "mgmt-anchor"},
            ))
    for rn in state.realnets:
        for host in rn.hosts or ["master"]:
            if rn.bridge:
                out.append(CleanupArtifact("interface", rn.bridge, host, lab, source="state"))
        if rn.router_container:
            out.append(CleanupArtifact("container", rn.router_container, "master", lab, source="state"))
    if state.mgmt:
        for host in state.scheduling or {}:
            out.extend([
                CleanupArtifact("interface", state.mgmt.vxlan_iface, host, lab, source="state"),
                CleanupArtifact("interface", state.mgmt.bridge, host, lab, source="state"),
                CleanupArtifact("interface", state.mgmt.vrf, host, lab, source="state"),
                CleanupArtifact("dnsmasq", f"/var/run/dnsmasq-{lab}.pid", host, lab, source="state"),
            ])
    for link in state.vxlan_dataplane:
        for side_name in ("side_a", "side_b"):
            side = getattr(link, side_name, {}) or {}
            host = side.get("node", "")
            iface = side.get("iface", "")
            if host and iface:
                out.append(CleanupArtifact("interface", f"vx-{iface}"[:15], host, lab, source="state"))
    return out


def collect_inventory(
    hosts: HostsConfig,
    states: dict[str, DeploymentState],
    *,
    clients: dict[str, SSHClient] | None = None,
) -> dict[str, HostCleanupInventory]:
    own_clients = clients is None
    if clients is None:
        clients = create_clients(hosts.all_hosts)
    known_labs = set(states)
    inventories: dict[str, HostCleanupInventory] = {}

    def _collect(name: str, client: SSHClient) -> HostCleanupInventory:
        inv = HostCleanupInventory(name=name, host=client.host)
        try:
            if own_clients:
                client.connect()
            inv.reachable = True
            inv.artifacts.extend(_list_containers(client, name, known_labs))
            inv.artifacts.extend(_list_networks(client, name, known_labs))
            inv.artifacts.extend(_list_interfaces(client, name, known_labs))
        except Exception as exc:
            inv.reachable = False
            inv.error = str(exc)
            log.warning("[%s] cleanup inventory failed: %s", name, exc)
        finally:
            if own_clients:
                client.close()
        return inv

    with ThreadPoolExecutor(max_workers=max(1, len(clients))) as pool:
        futures = {pool.submit(_collect, name, client): name for name, client in clients.items()}
        for future in as_completed(futures):
            inv = future.result()
            inventories[inv.name] = inv
    return inventories


def build_cleanup_plan(
    inventories: dict[str, HostCleanupInventory],
    states: dict[str, DeploymentState],
    *,
    grace_seconds: int,
) -> dict[str, LabCleanupPlan]:
    by_lab: dict[str, list[CleanupArtifact]] = {}
    for inv in inventories.values():
        for artifact in inv.artifacts:
            if artifact.lab:
                by_lab.setdefault(artifact.lab, []).append(artifact)
    for lab, state in states.items():
        by_lab.setdefault(lab, []).extend(expected_artifacts_from_state(state))

    plans: dict[str, LabCleanupPlan] = {}
    for lab in sorted(by_lab):
        plan = LabCleanupPlan(lab=lab)
        artifacts = _dedupe_artifacts(by_lab[lab])
        state = states.get(lab)
        expected_hosts = set((state.scheduling or {}).keys()) if state else set()
        if state:
            expected_hosts.update(a.host for a in expected_artifacts_from_state(state) if a.host)

        if _lab_has_running_vd(lab, artifacts, state):
            plan.protected = True
            plan.reasons.append("lab-runtime-running")
        for host in sorted(expected_hosts):
            inv = inventories.get(host)
            if inv is None or not inv.reachable:
                plan.protected = True
                plan.reasons.append(f"host-unreachable:{host}")
        if any(a.age_seconds is not None and a.age_seconds < grace_seconds for a in artifacts):
            plan.protected = True
            plan.reasons.append("artifact-inside-grace")

        if plan.protected:
            plans[lab] = plan
            continue

        for artifact in artifacts:
            action = _action_for_artifact(artifact, state)
            if action is None:
                if artifact.shared:
                    plan.warnings.append(f"shared artifact skipped: {artifact.name}")
                elif artifact.kind == "network":
                    plan.warnings.append(f"network skipped: {artifact.name}")
                elif artifact.kind == "interface" and artifact.source != "state":
                    plan.warnings.append(f"interface skipped without state: {artifact.name}")
                continue
            plan.actions.append(action)
        plans[lab] = plan
    return plans


def reconcile_once(
    hosts: HostsConfig,
    *,
    state_file: Path = DEFAULT_STATE_FILE,
    topologies_dir: Path | str = PATHS.topologies_dir,
    clients: dict[str, SSHClient] | None = None,
    dry_run: bool | None = None,
    grace_seconds: int | None = None,
) -> CleanupReport:
    started = time.monotonic()
    effective_dry_run = hosts.lab_cleanup.dry_run if dry_run is None else dry_run
    effective_grace = hosts.lab_cleanup.grace_seconds if grace_seconds is None else grace_seconds
    states = discover_state_files(topologies_dir)
    inventories = collect_inventory(hosts, states, clients=clients)
    plans = build_cleanup_plan(inventories, states, grace_seconds=effective_grace)
    report = CleanupReport(
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        dry_run=effective_dry_run,
        grace_seconds=effective_grace,
        scanned_hosts=inventories,
        labs=plans,
    )
    if not effective_dry_run:
        _execute_plans(plans, clients or create_clients(hosts.all_hosts), own_clients=clients is None)
    report.duration_ms = int((time.monotonic() - started) * 1000)
    write_state_file(state_file, report)
    return report


def write_state_file(path: Path, report: CleanupReport) -> None:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        tmp.replace(path)
    except OSError as exc:
        log.warning("Cannot write lab-cleanup state %s: %s", path, exc)


def read_state_file(path: Path = DEFAULT_STATE_FILE) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


class LabCleanupDaemon:
    def __init__(self, hosts: HostsConfig, state_file: Path = DEFAULT_STATE_FILE):
        self.hosts = hosts
        self.state_file = Path(state_file)
        self._stop = threading.Event()
        self._trigger = threading.Event()
        self._reconcile_count = 0

    def stop(self) -> None:
        self._stop.set()
        self._trigger.set()

    def trigger_reconcile(self) -> None:
        self._trigger.set()

    def run(self) -> None:
        if not self.hosts.lab_cleanup.enabled:
            log.info("lab-cleanup disabled in hosts.yml")
            return
        self._do_reconcile()
        interval = max(30, self.hosts.lab_cleanup.interval_seconds)
        while not self._stop.is_set():
            self._trigger.wait(timeout=interval)
            if self._stop.is_set():
                break
            self._trigger.clear()
            self._do_reconcile()

    def _do_reconcile(self) -> None:
        try:
            report = reconcile_once(self.hosts, state_file=self.state_file)
            self._reconcile_count += 1
            report.reconcile_count = self._reconcile_count
            write_state_file(self.state_file, report)
            actions = sum(len(p.actions) for p in report.labs.values())
            log.info("lab-cleanup reconcile #%d done: labs=%d actions=%d dry_run=%s",
                     self._reconcile_count, len(report.labs), actions, report.dry_run)
        except Exception:
            log.exception("lab-cleanup reconcile failed")


def _list_containers(client: SSHClient, host: str, known_labs: set[str]) -> list[CleanupArtifact]:
    rc, out, _ = client.run_no_check(
        "docker ps -a --format '{{.Names}}\t{{.State}}\t{{.CreatedAt}}'",
        timeout=20,
    )
    if rc != 0:
        return []
    artifacts = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[0]
        lab = parse_artifact_lab("container", name, known_labs)
        if not lab:
            continue
        artifacts.append(CleanupArtifact(
            kind="container",
            name=name,
            host=host,
            lab=lab,
            state=parts[1],
            age_seconds=_age_from_created_at(parts[2] if len(parts) > 2 else ""),
        ))
    return artifacts


def _list_networks(client: SSHClient, host: str, known_labs: set[str]) -> list[CleanupArtifact]:
    rc, out, _ = client.run_no_check("docker network ls --format '{{.Name}}'", timeout=20)
    if rc != 0:
        return []
    artifacts = []
    for name in out.splitlines():
        name = name.strip()
        if not name:
            continue
        shared = name in _SHARED_NETWORKS
        lab = parse_artifact_lab("network", name, known_labs)
        if not lab and not shared:
            continue
        containers = _network_container_count(client, name)
        artifacts.append(CleanupArtifact(
            kind="network",
            name=name,
            host=host,
            lab=lab,
            shared=shared,
            metadata={"containers": containers},
        ))
    return artifacts


def _list_interfaces(client: SSHClient, host: str, known_labs: set[str]) -> list[CleanupArtifact]:
    rc, out, _ = client.run_no_check("ip -o link show | awk -F': ' '{print $2}'", timeout=20)
    if rc != 0:
        return []
    artifacts = []
    for raw in out.splitlines():
        name = raw.split("@", 1)[0].strip()
        lab = parse_artifact_lab("interface", name, known_labs)
        if lab:
            artifacts.append(CleanupArtifact("interface", name, host, lab))
    return artifacts


def _network_container_count(client: SSHClient, name: str) -> int | None:
    rc, out, _ = client.run_no_check(
        f"docker network inspect -f '{{{{len .Containers}}}}' {name}",
        timeout=10,
    )
    if rc != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _action_for_artifact(
    artifact: CleanupArtifact,
    state: DeploymentState | None,
) -> CleanupAction | None:
    if artifact.shared:
        return None
    if artifact.kind == "container":
        if (
            artifact.state
            and artifact.state not in _CLEANABLE_CONTAINER_STATES
            and not _is_per_lab_container(artifact)
        ):
            return None
        return CleanupAction(
            action="remove-container",
            artifact=artifact,
            command=f"docker rm -f {shlex.quote(artifact.name)}",
            reason="stale dNLab lab container",
        )
    if artifact.kind == "network":
        containers = artifact.metadata.get("containers")
        if containers != 0:
            return None
        return CleanupAction(
            action="remove-network",
            artifact=artifact,
            command=f"docker network rm {shlex.quote(artifact.name)}",
            reason="empty per-lab docker network",
        )
    if artifact.kind == "interface":
        if state is None and artifact.source != "state":
            return None
        return CleanupAction(
            action="delete-interface",
            artifact=artifact,
            command=f"ip link delete {shlex.quote(artifact.name)}",
            reason="state-derived stale lab interface",
        )
    if artifact.kind == "dnsmasq":
        return CleanupAction(
            action="stop-dnsmasq",
            artifact=artifact,
            command=f"[ -f {shlex.quote(artifact.name)} ] && kill $(cat {shlex.quote(artifact.name)}) 2>/dev/null; rm -f {shlex.quote(artifact.name)}",
            reason="stale lab dnsmasq pid",
        )
    return None


def _is_mgmt_anchor_container(artifact: CleanupArtifact) -> bool:
    if artifact.kind != "container":
        return False
    if artifact.metadata.get("role") == "mgmt-anchor":
        return True
    return bool(_MGMT_ANCHOR_RE.match(artifact.name))


def _is_service_container(artifact: CleanupArtifact) -> bool:
    return artifact.kind == "container" and bool(_SERVICE_CONTAINER_RE.match(artifact.name))


def _is_per_lab_container(artifact: CleanupArtifact) -> bool:
    return artifact.kind == "container" and bool(artifact.lab)


def _expected_vd_container_names(lab: str, state: DeploymentState | None) -> set[str]:
    names: set[str] = set()
    if not state:
        return names
    for runtime in state.node_runtime.values():
        if runtime.container:
            names.add(runtime.container)
        if runtime.node:
            names.add(naming.vd_container_name(lab, runtime.node))
            names.add(naming.micro_vd_container_name(lab, runtime.node))
    for schedule in (state.scheduling or {}).values():
        for vd in schedule.vd:
            names.add(naming.vd_container_name(lab, vd))
            names.add(naming.micro_vd_container_name(lab, vd))
    return names


def _is_vd_container(
    artifact: CleanupArtifact,
    lab: str,
    state: DeploymentState | None,
    expected_names: set[str] | None = None,
) -> bool:
    if artifact.kind != "container":
        return False
    if _is_mgmt_anchor_container(artifact) or _is_service_container(artifact):
        return False
    expected_names = expected_names if expected_names is not None else _expected_vd_container_names(lab, state)
    if artifact.name in expected_names:
        return True
    if state:
        return False
    return (
        artifact.name.startswith(f"clab-{lab}-")
        or artifact.name.startswith(f"clab-dnlab-{lab}-")
    )


def _lab_has_running_vd(
    lab: str,
    artifacts: list[CleanupArtifact],
    state: DeploymentState | None,
) -> bool:
    # A successful deployment always persists multinode state. Live VD
    # containers without it are leftovers from an incomplete deploy or destroy;
    # treating them as authoritative would protect them forever.
    if state is None:
        return False
    expected_names = _expected_vd_container_names(lab, state)
    return any(
        artifact.state in _DOCKER_STATES_RUNNING
        and _is_vd_container(artifact, lab, state, expected_names)
        for artifact in artifacts
    )


def _execute_plans(
    plans: dict[str, LabCleanupPlan],
    clients: dict[str, SSHClient],
    *,
    own_clients: bool,
) -> None:
    connected: set[str] = set()
    try:
        if own_clients:
            for name, client in clients.items():
                try:
                    client.connect()
                    connected.add(name)
                except Exception:
                    log.exception("[%s] cannot connect for cleanup execution", name)
        else:
            connected = set(clients)
        for plan in plans.values():
            if plan.protected:
                continue
            for action in plan.actions:
                client = clients.get(action.artifact.host)
                if client is None or action.artifact.host not in connected:
                    action.executed = True
                    action.ok = False
                    action.error = "host unavailable"
                    continue
                action.executed = True
                rc, _, err = client.run_no_check(action.command, timeout=30)
                action.ok = rc == 0
                action.error = "" if rc == 0 else err.strip()
    finally:
        if own_clients:
            for name in connected:
                clients[name].close()


def _dedupe_artifacts(artifacts: list[CleanupArtifact]) -> list[CleanupArtifact]:
    merged: dict[tuple[str, str, str], CleanupArtifact] = {}
    for artifact in artifacts:
        key = (artifact.kind, artifact.host, artifact.name)
        prev = merged.get(key)
        if prev is None:
            merged[key] = artifact
            continue
        if artifact.state:
            prev.state = artifact.state
        if artifact.age_seconds is not None:
            prev.age_seconds = artifact.age_seconds
        if artifact.metadata:
            prev.metadata.update(artifact.metadata)
        if artifact.source == "state":
            prev.source = "state"
    return list(merged.values())


def _age_from_created_at(value: str) -> int | None:
    if not value:
        return None
    # Docker's CreatedAt includes a timezone abbreviation after a numeric
    # offset; the numeric offset is enough for fromisoformat.
    cleaned = re.sub(r"\s+[A-Z]{2,5}$", "", value.strip())
    try:
        created = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()))
