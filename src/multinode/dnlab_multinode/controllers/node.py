"""Single VD runtime lifecycle controller."""

from __future__ import annotations

import time
from pathlib import Path

from dnlab_multinode.models.state import DeploymentState
from dnlab_multinode.services import runtime_links as runtime_links_svc
from dnlab_multinode.services.config import parse_topology
from dnlab_multinode.services.ssh import create_clients
from dnlab_multinode.services.state import load_state, save_state


class NodeLifecycleError(Exception):
    pass


class NodeLifecycleController:
    def __init__(self, topology_file: str, *, hosts_file: str | None = None):
        self.topology_file = topology_file
        self.hosts_file = hosts_file
        self.topo = parse_topology(topology_file, hosts_file=hosts_file)
        self.state_dir = Path(topology_file).parent
        self.state = load_state(self.topo.name, self.state_dir)
        if not self.state:
            raise NodeLifecycleError(f"Lab '{self.topo.name}' is not deployed")

    def list_nodes(self) -> dict:
        return self.state.node_runtime

    def stop(self, node: str) -> DeploymentState:
        runtime = self._runtime(node)
        self._ensure_deployed()
        self._ensure_per_vd_runtime(runtime)
        if runtime.state == "stopped":
            return self.state

        clients = create_clients(self.topo.all_hosts)
        try:
            self._connect(clients)
            runtime.state = "stopping"
            save_state(self.state, self.state_dir)

            runtime_links_svc.delete_node_links(node, self.state.runtime_links, clients)
            clients[runtime.host].destroy_clab(runtime.topology_file)

            runtime.state = "stopped"
            runtime.last_error = ""
            for link in self.state.runtime_links:
                if node in self._link_nodes(link):
                    link.state = "partial"
            save_state(self.state, self.state_dir)
            return self.state
        except Exception as exc:
            runtime.state = "error"
            runtime.last_error = str(exc)
            save_state(self.state, self.state_dir)
            raise
        finally:
            for client in clients.values():
                client.close()

    def start(self, node: str) -> DeploymentState:
        runtime = self._runtime(node)
        self._ensure_deployed()
        self._ensure_per_vd_runtime(runtime)
        if runtime.state == "running":
            return self.state

        clients = create_clients(self.topo.all_hosts)
        try:
            self._connect(clients)
            runtime.state = "starting"
            save_state(self.state, self.state_dir)

            clients[runtime.host].deploy_clab(runtime.topology_file)
            self._wait_container_running(
                clients[runtime.host], runtime.container, timeout=60,
            )
            self._set_default_route(clients[runtime.host], runtime.container)

            runtime.state = "running"
            runtime.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            runtime.last_error = ""
            running = self._running_nodes()
            runtime_links_svc.reconcile_node_links(
                node, self.state.runtime_links, clients, self._underlay_ips(), running,
            )
            save_state(self.state, self.state_dir)
            return self.state
        except Exception as exc:
            runtime.state = "error"
            runtime.last_error = str(exc)
            save_state(self.state, self.state_dir)
            raise
        finally:
            for client in clients.values():
                client.close()

    def restart(self, node: str) -> DeploymentState:
        """Restart one VD using the established per-VD stop/start paths."""
        self.stop(node)
        return self.start(node)

    def reconcile(self, node: str | None = None) -> DeploymentState:
        self._ensure_deployed()
        clients = create_clients(self.topo.all_hosts)
        try:
            self._connect(clients)
            running = self._running_nodes()
            if node:
                self._ensure_per_vd_runtime(self._runtime(node))
                runtime_links_svc.reconcile_node_links(
                    node, self.state.runtime_links, clients, self._underlay_ips(), running,
                )
            else:
                runtime_links_svc.reconcile_all_links(
                    self.state.runtime_links, clients, self._underlay_ips(), running,
                )
            save_state(self.state, self.state_dir)
            return self.state
        finally:
            for client in clients.values():
                client.close()

    def _runtime(self, node: str):
        if node not in self.state.node_runtime:
            raise NodeLifecycleError(f"Unknown runtime VD '{node}'")
        return self.state.node_runtime[node]

    def _ensure_deployed(self) -> None:
        if not self.state.dnlab_deployed:
            raise NodeLifecycleError(f"Lab '{self.topo.name}' infrastructure is not deployed")

    def _ensure_per_vd_runtime(self, runtime) -> None:
        expected_prefix = f"clab-dnlab-{self.topo.name}-"
        if not runtime.container.startswith(expected_prefix):
            raise NodeLifecycleError(
                "Single-VD start/stop requires a per-VD runtime deployment. "
                f"Node '{runtime.node}' is part of a legacy per-host Containerlab topology."
            )

    @staticmethod
    def _connect(clients) -> None:
        for client in clients.values():
            client.connect()

    @staticmethod
    def _wait_container_running(client, container: str, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc, out, _ = client.run_no_check(
                f"docker inspect --format '{{{{.State.Status}}}}' {container}",
                timeout=15,
            )
            if rc == 0 and out.strip() == "running":
                return
            if rc == 0 and out.strip() in {"exited", "dead"}:
                raise NodeLifecycleError(f"Container {container} is {out.strip()}")
            time.sleep(5)
        raise NodeLifecycleError(f"Timeout waiting for {container} to run")

    def _running_nodes(self) -> set[str]:
        return {
            node
            for node, runtime in self.state.node_runtime.items()
            if runtime.state == "running"
        }

    def _set_default_route(self, client, container: str) -> None:
        gw = self.topo.mgmt.ipv4_gw
        if not gw:
            return
        client.run_no_check(
            f"docker exec {container} sh -lc 'ip route replace default via {gw}'",
            timeout=15,
        )

    def _underlay_ips(self) -> dict[str, str]:
        return {
            host: schedule.host
            for host, schedule in self.state.scheduling.items()
        }

    @staticmethod
    def _link_nodes(link) -> set[str]:
        return {
            endpoint.get("node")
            for endpoint in [link.endpoint_a, link.endpoint_b]
            if endpoint.get("node")
        }
