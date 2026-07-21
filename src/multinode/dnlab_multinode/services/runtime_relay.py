"""Runtime relay sidecar management.

One relay runs on every host that owns VDs for a lab. It exposes only lab
allowlisted console/log operations to jumphost and GUI clients.
"""

from __future__ import annotations

import logging
import shlex
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed

from dnlab_multinode.models.schedule import SchedulePlan
from dnlab_multinode.models.topology import DistributedTopology
from dnlab_multinode.services.images import image_for
from dnlab_multinode.services.ssh import SSHClient
from dnlab_multinode.utils.ids import runtime_relay_port
from dnlab_multinode.utils.naming import (
    micro_vd_container_name,
    runtime_relay_container_name,
)

log = logging.getLogger(__name__)

_CAPABILITY_LABEL = "io.dnlab.runtime-relay.capabilities"
_PREFIX_CAPABILITY = "lab-prefix-console-port-v1"
_MULTISESSION_CAPABILITY = "multisession-console-v1"
_REQUIRED_CAPABILITIES = {_PREFIX_CAPABILITY, _MULTISESSION_CAPABILITY}


def _capabilities(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def _containers_for_host(
    lab_name: str, plan: SchedulePlan, host_name: str,
) -> list[str]:
    assignment = plan.assignments.get(host_name)
    if not assignment:
        return []
    return [micro_vd_container_name(lab_name, vd) for vd in assignment.vd_names]


def _allowed_prefix(lab_name: str) -> str:
    """Return the namespace shared by all per-VD containers in one lab."""
    return f"clab-dnlab-{lab_name}-"


def _deploy_runtime_relay(
    topo: DistributedTopology,
    host_name: str,
    containers: list[str],
    client: SSHClient,
    bind_ip: str,
    api_key: str,
) -> dict:
    port = runtime_relay_port(topo.name)
    relay_container = runtime_relay_container_name(topo.name)
    image = image_for("runtime-relay")

    rc, _, _ = client.run_no_check(f"docker image inspect {image} >/dev/null 2>&1")
    if rc != 0:
        raise RuntimeError(
            f"[{host_name}] Runtime relay image '{image}' not found. "
            "Preload release images with `docker compose --profile "
            "release-images pull` on the master, then verify image-sync "
            "to workers."
        )

    client.run(f"docker rm -f {relay_container} 2>/dev/null", check=False)
    allowed = " ".join(containers)
    run_cmd = (
        "docker run -d "
        f"--name {relay_container} "
        "--restart unless-stopped "
        "--network host "
        "-v /var/run/docker.sock:/var/run/docker.sock "
        f"-e RELAY_BIND={shlex.quote(bind_ip)} "
        f"-e RELAY_PORT={port} "
        f"-e RELAY_API_KEY={shlex.quote(api_key)} "
        f"-e RELAY_ALLOWED_CONTAINERS={shlex.quote(allowed)} "
        f"-e RELAY_ALLOWED_PREFIX={shlex.quote(_allowed_prefix(topo.name))} "
        f"{image}"
    )
    client.run(run_cmd)
    rc, out, _ = client.run_no_check(
        f"docker inspect -f '{{{{.State.Running}}}}' {relay_container}"
    )
    if rc != 0 or out.strip() != "true":
        _, logs, _ = client.run_no_check(
            f"docker logs {relay_container} 2>&1 | tail -40"
        )
        client.run(f"docker rm -f {relay_container} 2>/dev/null", check=False)
        raise RuntimeError(
            f"[{host_name}] Runtime relay '{relay_container}' failed to start.\n{logs}"
        )
    return {
        "container": relay_container,
        "bind_ip": bind_ip,
        "port": port,
        "api_key": api_key,
        "allowed": containers,
    }


def deploy_runtime_relays(
    topo: DistributedTopology,
    plan: SchedulePlan,
    clients: dict[str, SSHClient],
    underlay_ips: dict[str, str],
    api_key: str,
) -> dict[str, dict]:
    results: dict[str, dict] = {}

    def _deploy_one(host_name: str):
        containers = _containers_for_host(topo.name, plan, host_name)
        client = clients[host_name]
        relay_container = runtime_relay_container_name(topo.name)
        if not containers:
            client.run(f"docker rm -f {relay_container} 2>/dev/null", check=False)
            return host_name, None
        bind_ip = underlay_ips[host_name]
        return host_name, _deploy_runtime_relay(
            topo, host_name, containers, client, bind_ip, api_key,
        )

    with ThreadPoolExecutor(max_workers=max(1, len(clients))) as pool:
        futures = {pool.submit(_deploy_one, h): h for h in clients}
        for future in as_completed(futures):
            host = futures[future]
            try:
                host_name, info = future.result()
            except Exception as exc:
                raise RuntimeError(f"[{host}] runtime relay deploy failed: {exc}") from exc
            if info:
                results[host_name] = info
    return results


def reconcile_runtime_relays(
    topo: DistributedTopology,
    plan: SchedulePlan,
    clients: dict[str, SSHClient],
    underlay_ips: dict[str, str],
    api_key: str,
    current: dict[str, object],
) -> dict[str, dict]:
    """Refresh relay metadata without interrupting existing consoles.

    New relays authorize the whole lab's per-VD container namespace, so adding
    a node only changes persisted inventory. A relay from an older release is
    recreated once when it lacks a required protocol/runtime capability.
    """
    results: dict[str, dict] = {}
    relay_container = runtime_relay_container_name(topo.name)
    port = runtime_relay_port(topo.name)

    for host_name, client in clients.items():
        containers = _containers_for_host(topo.name, plan, host_name)
        previous = current.get(host_name)
        if not containers:
            if previous:
                client.run(f"docker rm -f {relay_container} 2>/dev/null", check=False)
            continue

        running = False
        if previous:
            rc, out, _ = client.run_no_check(
                f"docker inspect -f '{{{{.State.Running}}}}' {relay_container}"
            )
            running = rc == 0 and out.strip() == "true"
        if previous and running:
            rc, capability, _ = client.run_no_check(
                "docker inspect -f "
                f"'{{{{index .Config.Labels \"{_CAPABILITY_LABEL}\"}}}}' "
                f"{relay_container}"
            )
            supported = _capabilities(capability) if rc == 0 else set()
            if _REQUIRED_CAPABILITIES <= supported:
                results[host_name] = {
                    "container": getattr(previous, "container", relay_container),
                    "bind_ip": getattr(previous, "bind_ip", underlay_ips[host_name]),
                    "port": getattr(previous, "port", port),
                    "api_key": getattr(previous, "api_key", api_key),
                    "allowed": containers,
                }
                continue

        results[host_name] = _deploy_runtime_relay(
            topo, host_name, containers, client, underlay_ips[host_name], api_key,
        )
    return results


def destroy_runtime_relays(lab_name: str, clients: dict[str, SSHClient]) -> None:
    container = runtime_relay_container_name(lab_name)
    for host_name, client in clients.items():
        try:
            client.run(f"docker rm -f {container} 2>/dev/null", check=False)
            log.info("[%s] Runtime relay removed: %s", host_name, container)
        except Exception as exc:
            log.error("[%s] Failed to remove runtime relay: %s", host_name, exc)
