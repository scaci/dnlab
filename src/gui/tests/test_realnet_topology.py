from types import SimpleNamespace
import asyncio

import yaml
import pytest

from app.config import settings
from app.controllers.topology_controller import TopologyController
from app.models.node import Node
from app.models.topology import Topology
from app.services.containerlab_service import ContainerLabService
from app.services import realnet_bgp
from app.views.api import admin_routes


def test_realnet_config_is_nested_for_multinode_parser(tmp_path):
    topo = Topology(
        name="lab",
        nodes=[
            Node(
                name="real_net1",
                kind="_real_net",
                image="",
                extra={
                    "ipv4": "192.168.50.1/24",
                    "nat": False,
                    "ospf": True,
                    "description": "wan breakout",
                },
            )
        ],
    )

    path = tmp_path / "lab.yml"
    ContainerLabService().save_topology_to(path, topo)

    data = yaml.safe_load(path.read_text())
    real_net = data["topology"]["nodes"]["real_net1"]

    assert real_net["kind"] == "_real_net"
    assert real_net["extra"] == {
        "ipv4": "192.168.50.1/24",
        "nat": False,
        "ospf": True,
        "description": "wan breakout",
    }
    assert "ipv4" not in real_net
    assert "env" not in real_net


def test_realnet_loader_unwraps_nested_extra(tmp_path):
    path = tmp_path / "lab.yml"
    path.write_text(
        """
name: lab
topology:
  nodes:
    real_net1:
      kind: _real_net
      image: ""
      extra:
        ipv4: 192.168.50.1/24
        nat: false
        ospf: true
  links: []
"""
    )

    topo = ContainerLabService().load_topology_from_file(path)

    node = topo.get_node("real_net1")
    assert node is not None
    assert node.extra == {
        "ipv4": "192.168.50.1/24",
        "nat": False,
        "ospf": True,
    }


def test_realnet_loader_accepts_legacy_flat_config(tmp_path):
    path = tmp_path / "lab.yml"
    path.write_text(
        """
name: lab
topology:
  nodes:
    real_net1:
      kind: _real_net
      image: ""
      ipv4: 192.168.50.1/24
      nat: false
      ospf: true
  links: []
"""
    )

    topo = ContainerLabService().load_topology_from_file(path)

    node = topo.get_node("real_net1")
    assert node is not None
    assert node.extra == {
        "ipv4": "192.168.50.1/24",
        "nat": False,
        "ospf": True,
    }


def test_realnet_bgp_status_allows_incomplete_read_only_config():
    status = realnet_bgp.realnet_bgp_status({})

    assert status == {
        "configured": False,
        "skipped": True,
        "reason": "RealNet BGP RR IP/Host network not configured",
    }


def test_realnet_rr_startup_does_not_write_hosts_file(tmp_path, monkeypatch):
    hosts = tmp_path / "hosts.yml"
    content = """
infrastructure:
  master:
    host: 127.0.0.1
    ssh_user: root
"""
    hosts.write_text(content, encoding="utf-8")
    monkeypatch.setattr(settings, "DNLAB_MULTINODE_HOSTS", str(hosts))
    monkeypatch.setattr(settings, "DNLAB_MULTINODE_API_URL", "http://dnlab-multinode:8081")
    monkeypatch.setattr(
        realnet_bgp,
        "_ensure_route_reflector_via_api",
        lambda hosts_path: {"ok": False, "skipped": True},
    )

    result = realnet_bgp.ensure_route_reflector_from_hosts()

    assert result == {"ok": False, "skipped": True}
    assert hosts.read_text(encoding="utf-8") == content


def test_realnet_bgp_password_remains_admin_editable():
    model = SimpleNamespace(data=SimpleNamespace(extra_infrastructure={"realnet": {"rr_password": "oldsecret"}}))
    payload = {
        "rr_as": 64512,
        "rr_ip": "10.0.0.10",
        "host_net": "10.0.0.0/24",
        "router_as_pool": "64513-64520",
        "router_ip_pool": "10.0.0.20-10.0.0.30",
        "realnet_network_pool": "100.64.0.0/10",
        "rr_password": "newsecret",
    }

    updated, cfg = realnet_bgp.update_hosts_model_realnet_bgp(model, payload)

    assert cfg.rr_password == "newsecret"
    assert updated.data.extra_infrastructure["realnet"]["rr_password"] == "newsecret"


def test_realnet_bgp_password_is_preserved_when_admin_leaves_it_blank():
    model = SimpleNamespace(data=SimpleNamespace(extra_infrastructure={"realnet": {"rr_password": "oldsecret"}}))
    payload = {
        "rr_as": 64512,
        "rr_ip": "10.0.0.10",
        "host_net": "10.0.0.0/24",
        "router_as_pool": "64513-64520",
        "router_ip_pool": "10.0.0.20-10.0.0.30",
        "realnet_network_pool": "100.64.0.0/10",
        "rr_password": "",
    }

    updated, cfg = realnet_bgp.update_hosts_model_realnet_bgp(model, payload)

    assert cfg.rr_password == "oldsecret"
    assert updated.data.extra_infrastructure["realnet"]["rr_password"] == "oldsecret"


def test_realnet_bgp_rr_password_route_uses_multinode_serializer(tmp_path, monkeypatch):
    hosts = tmp_path / "hosts.yml"
    hosts.write_text(
        """
infrastructure:
  realnet:
    rr_as: 64512
    rr_ip: 10.0.0.10
    host_net: 10.0.0.0/24
    router_as_pool: 64513-64520
    router_ip_pool: 10.0.0.20-10.0.0.30
    realnet_network_pool: 100.64.0.0/10
    rr_password: oldsecret
""",
        encoding="utf-8",
    )
    serialize_calls = []

    async def fake_serialize(key, model):
        serialize_calls.append((key, model.data.extra_infrastructure["realnet"]["rr_password"]))
        return "serialized-hosts\n"

    async def fake_audit_record(*args, **kwargs):
        return None

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    class FakeDb:
        async def commit(self):
            return None

    monkeypatch.setattr(admin_routes, "_hosts_file", lambda: hosts)
    monkeypatch.setattr(admin_routes, "_serialize_config_model", fake_serialize)
    monkeypatch.setattr(admin_routes, "_atomic_write", lambda path, content: None)
    monkeypatch.setattr(admin_routes.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(admin_routes.audit, "record", fake_audit_record)
    monkeypatch.setattr(
        admin_routes.realnet_bgp,
        "ensure_route_reflector_from_hosts",
        lambda: {"ok": True, "skipped": False},
    )

    result = asyncio.run(
        admin_routes.regenerate_realnet_bgp_rr_password(
            request=SimpleNamespace(),
            admin=SimpleNamespace(),
            db=FakeDb(),
        )
    )

    assert serialize_calls
    assert serialize_calls[0][0] == "hosts"
    assert serialize_calls[0][1] != "oldsecret"
    assert result["data"]["rr_password"] == serialize_calls[0][1]


def test_realnet_bgp_enable_requires_admin_config(tmp_path, monkeypatch):
    hosts = tmp_path / "hosts.yml"
    hosts.write_text(
        """
infrastructure:
  realnet:
    router_as_pool: 64513-64520
""",
        encoding="utf-8",
    )
    topo_path = tmp_path / "lab.yml"
    ContainerLabService().save_topology_to(
        topo_path,
        Topology(
            name="lab",
            nodes=[
                Node(
                    name="net1",
                    kind="_real_net",
                    image="",
                    extra={"network": "", "ipv4": "", "nat": True, "bgp": False},
                )
            ],
        ),
    )
    monkeypatch.setattr(settings, "DNLAB_MULTINODE_HOSTS", str(hosts))
    monkeypatch.setattr(settings, "TOPOLOGIES_DIR", tmp_path)

    with pytest.raises(realnet_bgp.RealNetBgpError, match="Configure Admin > RealNet BGP"):
        TopologyController().update_node_by_path(
            topo_path,
            "lab",
            "net1",
            {"extra": {"bgp": True, "nat": False}},
        )


def test_realnet_bgp_enable_saves_allocated_router_settings(tmp_path, monkeypatch):
    hosts = tmp_path / "hosts.yml"
    hosts.write_text(
        """
infrastructure:
  realnet:
    rr_as: 64512
    rr_ip: 10.0.0.10
    host_net: 10.0.0.0/24
    router_as_pool: 64513-64520
    router_ip_pool: 10.0.0.20-10.0.0.30
    realnet_network_pool: 100.64.0.0/10
    rr_password: rrsecret
""",
        encoding="utf-8",
    )
    topo_path = tmp_path / "lab.yml"
    ContainerLabService().save_topology_to(
        topo_path,
        Topology(
            name="lab",
            nodes=[
                Node(
                    name="net1",
                    kind="_real_net",
                    image="",
                    extra={"network": "", "ipv4": "", "nat": True, "bgp": False},
                )
            ],
        ),
    )
    monkeypatch.setattr(settings, "DNLAB_MULTINODE_HOSTS", str(hosts))
    monkeypatch.setattr(settings, "TOPOLOGIES_DIR", tmp_path)

    topo = TopologyController().update_node_by_path(
        topo_path,
        "lab",
        "net1",
        {"extra": {"bgp": True, "nat": False}},
    )

    extra = topo.get_node("net1").extra
    assert extra["bgp"] is True
    assert extra["nat"] is False
    assert extra["bgp_as"] == 64513
    assert extra["bgp_router_ip"] == "10.0.0.20"
    assert extra["bgp_password"]
