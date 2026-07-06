"""Shared pytest fixtures."""

import pytest

from dnlab_multinode.models.topology import (
    DistributedTopology, InfraHost, VDNode, Link, MgmtConfig, JumphostConfig,
    JumphostNet,
)
from dnlab_multinode.models.schedule import VDResources, HostResources


def make_topology(
    name: str = "lab",
    nodes: dict | None = None,
    links: list | None = None,
    num_workers: int = 2,
) -> DistributedTopology:
    master = InfraHost(
        name="master", host="10.0.0.10",
        ssh_user="root", ssh_key="~/.ssh/id",
        is_master=True,
    )
    workers = {
        f"worker{i+1}": InfraHost(
            name=f"worker{i+1}", host=f"10.0.0.{11+i}",
            ssh_user="root", ssh_key="~/.ssh/id",
        )
        for i in range(num_workers)
    }
    if nodes is None:
        nodes = {
            "R1": VDNode(name="R1", kind="linux", image="alpine"),
            "R2": VDNode(name="R2", kind="linux", image="alpine"),
        }
    if links is None:
        links = [Link(source="R1", source_iface="eth1", target="R2", target_iface="eth1")]

    return DistributedTopology(
        name=name,
        master=master,
        workers=workers,
        underlay_iface="eth0",
        jumphost=JumphostConfig(image="jh:latest"),
        jumphost_net=JumphostNet(
            network="jh-net", bridge="jh-br",
            ipv4_subnet="10.100.0.0/24", ipv4_gw="10.100.0.1",
        ),
        nodes=nodes,
        links=links,
        mgmt=MgmtConfig(
            network=f"mgmt-{name}", bridge=f"br-{name}",
            ipv4_subnet="172.20.0.0/24", ipv4_gw="172.20.0.1",
        ),
    )


def make_vd_resources(specs: dict[str, tuple[int, int]]) -> dict[str, VDResources]:
    """specs: {node_name: (cpu, ram_mb)}"""
    return {
        name: VDResources(name=name, image="x", cpu=cpu, ram_mb=ram)
        for name, (cpu, ram) in specs.items()
    }


def make_host_resources(specs: dict[str, tuple[int, int]]) -> dict[str, HostResources]:
    """specs: {host_name: (cpu, ram_mb)}"""
    return {
        name: HostResources(
            name=name, host=f"10.0.0.{10+i}",
            cpu_available=cpu, ram_mb_available=ram,
            is_master=(name == "master"),
        )
        for i, (name, (cpu, ram)) in enumerate(specs.items())
    }


@pytest.fixture
def topo_factory():
    return make_topology


@pytest.fixture
def vd_factory():
    return make_vd_resources


@pytest.fixture
def host_factory():
    return make_host_resources
