import dataclasses
import json

import pytest
import yaml

from dnlab_multinode.services.config import (
    ConfigError, assign_sticky_mgmt_ipv4, parse_topology,
)


def _hosts_file(tmp_path):
    hosts = tmp_path / "hosts.yml"
    hosts.write_text("""
infrastructure:
  master:
    host: 10.0.0.10
    ssh_user: root
    ssh_key: ~/.ssh/id
  workers: {}
  underlay_iface: eth0
defaults:
  mgmt:
    ipv4_subnet: 172.20.20.0/24
    ipv4_gw: 172.20.20.1
""")
    return hosts


def _topology_file(tmp_path, name="lab", mgmt=None):
    data = {
        "name": name,
        "topology": {
            "nodes": {
                "r1": {"kind": "linux", "image": "alpine"},
            },
        },
    }
    if mgmt is not None:
        data["mgmt"] = mgmt
    topo = tmp_path / f"{name}.yml"
    topo.write_text(yaml.safe_dump(data))
    return topo


def _topology_file_with_node_mgmt(tmp_path, mgmt_ipv4):
    data = {
        "name": "lab",
        "mgmt": {"ipv4-subnet": "172.20.20.0/24"},
        "topology": {
            "nodes": {
                "r1": {
                    "kind": "linux",
                    "image": "alpine",
                    "mgmt-ipv4": mgmt_ipv4,
                },
            },
        },
    }
    topo = tmp_path / "reserved.yml"
    topo.write_text(yaml.safe_dump(data))
    return topo


def test_default_mgmt_subnet_moves_when_active_subnet_is_busy(tmp_path, monkeypatch):
    from dnlab_multinode.services import config

    monkeypatch.setattr(
        config,
        "_active_mgmt_networks",
        lambda current_lab: [config.ipaddress.ip_network("172.20.20.0/24")],
    )

    topo = parse_topology(_topology_file(tmp_path), hosts_file=_hosts_file(tmp_path))

    assert topo.mgmt.ipv4_subnet == "172.20.21.0/24"
    assert topo.mgmt.docker_ipv4_gw == "172.20.21.1"
    assert topo.mgmt.ipv4_gw == "172.20.21.254"
    assert topo.mgmt.ipv6_subnet == "3fff:172:20:21::/64"
    assert topo.mgmt.ipv6_gw == "3fff:172:20:21:ffff:ffff:ffff:ffff"


def test_custom_mgmt_subnet_overlap_raises(tmp_path, monkeypatch):
    from dnlab_multinode.services import config

    monkeypatch.setattr(
        config,
        "_active_mgmt_networks",
        lambda current_lab: [config.ipaddress.ip_network("172.20.20.0/24")],
    )

    with pytest.raises(ConfigError, match="overlaps with active lab"):
        parse_topology(
            _topology_file(tmp_path, mgmt={
                "ipv4-subnet": "172.20.20.0/24",
                "ipv4-gw": "172.20.20.1",
            }),
            hosts_file=_hosts_file(tmp_path),
        )


def test_current_lab_active_state_is_ignored(tmp_path, monkeypatch):
    from dnlab_multinode.services import config

    states = tmp_path / "states"
    states.mkdir()
    (states / ".lab.multinode.json").write_text(json.dumps({
        "lab_name": "lab",
        "topology_file": "lab.yml",
        "mgmt": {"subnet": "172.20.20.0/24"},
    }))

    monkeypatch.setattr(config, "PATHS", dataclasses.replace(
        config.PATHS,
        topologies_dir=str(states),
    ))

    topo = parse_topology(
        _topology_file(tmp_path, mgmt={
            "ipv4-subnet": "172.20.20.0/24",
            "ipv4-gw": "172.20.20.1",
        }),
        hosts_file=_hosts_file(tmp_path),
    )

    assert topo.mgmt.ipv4_subnet == "172.20.20.0/24"
    assert topo.mgmt.ipv4_gw == "172.20.20.254"


def test_sticky_mgmt_reservations_survive_node_set_changes(tmp_path, monkeypatch):
    from dnlab_multinode.services import config

    monkeypatch.setattr(config, "_active_mgmt_networks", lambda current_lab: [])

    data = {
        "name": "lab",
        "mgmt": {"ipv4-subnet": "172.20.20.0/24"},
        "topology": {
            "nodes": {
                "aaa-new": {"kind": "linux", "image": "alpine"},
                "r1": {"kind": "linux", "image": "alpine"},
            },
        },
    }
    topo_file = tmp_path / "lab.yml"
    topo_file.write_text(yaml.safe_dump(data))

    topo = parse_topology(topo_file, hosts_file=_hosts_file(tmp_path))
    reservations = assign_sticky_mgmt_ipv4(
        topo.nodes,
        topo.mgmt,
        {
            "r1": "172.20.20.20",
            "removed-node": "172.20.20.21",
        },
    )

    assert topo.nodes["r1"].mgmt_ipv4 == "172.20.20.20"
    assert topo.nodes["aaa-new"].mgmt_ipv4 not in {
        "172.20.20.20",
        "172.20.20.21",
    }
    assert reservations["removed-node"] == "172.20.20.21"


def test_custom_ipv6_subnet_sets_last_address_as_gateway(tmp_path):
    topo = parse_topology(
        _topology_file(tmp_path, mgmt={
            "ipv4-subnet": "172.20.30.0/24",
            "ipv6-subnet": "2001:db8:30::/120",
        }),
        hosts_file=_hosts_file(tmp_path),
    )

    assert topo.mgmt.ipv6_subnet == "2001:db8:30::/120"
    assert topo.mgmt.ipv6_gw == "2001:db8:30::ff"


def test_legacy_ipv4_mapped_ipv6_subnet_is_normalized(tmp_path):
    topo = parse_topology(
        _topology_file(tmp_path, mgmt={
            "ipv4-subnet": "172.20.21.0/24",
            "ipv6-subnet": "::ffff:172.20.21.0/120",
            "ipv6-gw": "::ffff:172.20.21.255",
        }),
        hosts_file=_hosts_file(tmp_path),
    )

    assert topo.mgmt.ipv6_subnet == "3fff:172:20:21::/64"
    assert topo.mgmt.ipv6_gw == "3fff:172:20:21:ffff:ffff:ffff:ffff"


def test_too_small_mgmt_subnet_raises(tmp_path):
    with pytest.raises(ConfigError, match="too small"):
        parse_topology(
            _topology_file(tmp_path, mgmt={"ipv4-subnet": "172.20.30.0/30"}),
            hosts_file=_hosts_file(tmp_path),
        )


def test_invalid_ipv6_subnet_raises(tmp_path):
    with pytest.raises(ConfigError, match="Invalid mgmt.ipv6-subnet"):
        parse_topology(
            _topology_file(tmp_path, mgmt={
                "ipv4-subnet": "172.20.30.0/24",
                "ipv6-subnet": "not-ipv6",
            }),
            hosts_file=_hosts_file(tmp_path),
        )


@pytest.mark.parametrize("ip", [
    "172.20.20.1",
    "172.20.20.252",
    "172.20.20.253",
    "172.20.20.254",
])
def test_explicit_node_mgmt_ip_cannot_use_reserved_addresses(tmp_path, monkeypatch, ip):
    from dnlab_multinode.services import config

    monkeypatch.setattr(config, "_active_mgmt_networks", lambda current_lab: [])

    with pytest.raises(ConfigError, match="reserved"):
        parse_topology(
            _topology_file_with_node_mgmt(tmp_path, ip),
            hosts_file=_hosts_file(tmp_path),
        )
