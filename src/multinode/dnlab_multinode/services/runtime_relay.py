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
from dnlab_multinode.utils.naming import micro_vd_container_name, runtime_relay_container_name

log = logging.getLogger(__name__)

def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def _containers_for_host(
    lab_name: str,
    plan: SchedulePlan,
    host_name: str,
    runtime_containers: dict[str, str] | None = None,
) -> list[str]:
    assignment = plan.assignments.get(host_name)
    if not assignment:
        return []
    return [
        (runtime_containers or {}).get(vd, micro_vd_container_name(lab_name, vd))
        for vd in assignment.vd_names
    ]


def deploy_runtime_relays(
    topo: DistributedTopology,
    plan: SchedulePlan,
    clients: dict[str, SSHClient],
    underlay_ips: dict[str, str],
    api_key: str,
    runtime_containers: dict[str, str] | None = None,
) -> dict[str, dict]:
    port = runtime_relay_port(topo.name)
    results: dict[str, dict] = {}

    def _deploy_one(host_name: str):
        containers = _containers_for_host(
            topo.name,
            plan,
            host_name,
            runtime_containers=runtime_containers,
        )
        if not containers:
            return host_name, None
        client = clients[host_name]
        relay_container = runtime_relay_container_name(topo.name)
        image = image_for("runtime-relay")

        rc, _, _ = client.run_no_check(
            f"docker image inspect {image} >/dev/null 2>&1"
        )
        if rc != 0:
            raise RuntimeError(
                f"[{host_name}] Runtime relay image '{image}' not found. "
                "Preload release images with `docker compose --profile "
                "release-images pull` on the master, then verify image-sync "
                "to workers."
            )

        client.run(f"docker rm -f {relay_container} 2>/dev/null", check=False)
        allowed = " ".join(containers)
        bind_ip = underlay_ips[host_name]
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
            f"{image}"
        )
        client.run(run_cmd)
        rc, out, _ = client.run_no_check(
            f"docker inspect -f '{{{{.State.Running}}}}' {relay_container}"
        )
        if rc != 0 or out.strip() != "true":
            _, logs, _ = client.run_no_check(f"docker logs {relay_container} 2>&1 | tail -40")
            client.run(f"docker rm -f {relay_container} 2>/dev/null", check=False)
            raise RuntimeError(
                f"[{host_name}] Runtime relay '{relay_container}' failed to start.\n{logs}"
            )
        return host_name, {
            "container": relay_container,
            "bind_ip": bind_ip,
            "port": port,
            "api_key": api_key,
            "allowed": containers,
        }

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


def destroy_runtime_relays(lab_name: str, clients: dict[str, SSHClient]) -> None:
    container = runtime_relay_container_name(lab_name)
    for host_name, client in clients.items():
        try:
            client.run(f"docker rm -f {container} 2>/dev/null", check=False)
            log.info("[%s] Runtime relay removed: %s", host_name, container)
        except Exception as exc:
            log.error("[%s] Failed to remove runtime relay: %s", host_name, exc)
