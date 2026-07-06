"""Live lifecycle operations for RealNet infrastructure nodes."""

from __future__ import annotations

from pathlib import Path

from dnlab_multinode.models.state import DeploymentState
from dnlab_multinode.services import realnet as realnet_svc
from dnlab_multinode.services.config import parse_topology
from dnlab_multinode.services.ssh import SSHClient
from dnlab_multinode.services.state import load_state, save_state


class RealNetLifecycleError(Exception):
    pass


class RealNetLifecycleController:
    """Reconcile one deployed RealNet router without restarting the lab."""

    def __init__(self, topology_file: str, *, hosts_file: str | None = None):
        self.topology_file = topology_file
        self.hosts_file = hosts_file
        self.topo = parse_topology(topology_file, hosts_file=hosts_file)
        self.state_dir = Path(topology_file).parent
        self.state = load_state(self.topo.name, self.state_dir)
        if not self.state:
            raise RealNetLifecycleError(f"Lab '{self.topo.name}' is not deployed")

    def reconcile(self, realnet_name: str) -> DeploymentState:
        if not self.state.dnlab_deployed:
            raise RealNetLifecycleError(f"Lab '{self.topo.name}' infrastructure is not deployed")
        if realnet_name not in self.topo.real_nets:
            raise RealNetLifecycleError(f"Unknown real_net '{realnet_name}'")

        rn = self.topo.real_nets[realnet_name]
        rn_state = self._realnet_state(realnet_name)
        master = SSHClient(
            host=self.topo.master.host,
            user=self.topo.master.ssh_user,
            key_path=self.topo.master.ssh_key,
            name=self.topo.master.name,
        )
        master.connect()
        try:
            if rn.bgp:
                realnet_svc.deploy_route_reflector(self.topo, master)
            else:
                realnet_svc.ensure_realnet_wan_network(master, self.topo)

            rn_state.lan_ipv4 = rn.ipv4
            rn_state.nat = rn.nat and not rn.bgp
            rn_state.bgp = rn.bgp
            rn_state.bgp_as = rn.bgp_as
            rn_state.bgp_router_ip = rn.bgp_router_ip
            realnet_svc.deploy_router(self.topo, rn, rn_state, master)
            save_state(self.state, self.state_dir)
            return self.state
        finally:
            master.close()

    def _realnet_state(self, realnet_name: str):
        for rn_state in self.state.realnets:
            if rn_state.name == realnet_name:
                return rn_state
        raise RealNetLifecycleError(
            f"real_net '{realnet_name}' has no deployed state. "
            "Deploy the lab once before live-reconciling RealNet."
        )
