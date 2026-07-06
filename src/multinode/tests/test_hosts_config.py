"""Tests for hosts.yml parsing — focused on the jumphost_net SSH fields."""

import pytest

from dnlab_multinode.services.hosts_config import (
    HostsConfigError, _parse_hosts_dict,
)


def _base_raw(**jh_net_overrides) -> dict:
    """Minimal hosts.yml dict with optional jumphost_net overrides."""
    return {
        "infrastructure": {
            "master": {"host": "10.0.0.1", "ssh_user": "root"},
            "workers": {},
            "underlay_iface": "eth0",
            "jumphost_net": {
                "network": "dnlab-jh",
                "bridge": "br-jh",
                "ipv4_subnet": "192.168.100.0/24",
                "ipv4_gw": "192.168.100.1",
                **jh_net_overrides,
            },
        },
    }


def test_ssh_port_range_and_bind_ip_defaults():
    cfg = _parse_hosts_dict(_base_raw())
    assert cfg.jumphost_net.ssh_port_range == "2200-2299"
    assert cfg.jumphost_net.ssh_bind_ip == "0.0.0.0"


def test_ssh_port_range_custom():
    cfg = _parse_hosts_dict(_base_raw(ssh_port_range="3000-3099"))
    assert cfg.jumphost_net.ssh_port_range == "3000-3099"


def test_ssh_bind_ip_custom():
    cfg = _parse_hosts_dict(_base_raw(ssh_bind_ip="10.20.30.40"))
    assert cfg.jumphost_net.ssh_bind_ip == "10.20.30.40"


def test_persistence_defaults_to_local_sticky():
    cfg = _parse_hosts_dict(_base_raw())
    assert cfg.persistence.backend == "local-sticky"
    assert cfg.persistence.root == "/var/lib/docker/dnlab-backups"
    assert cfg.persistence.allow_migration_fallback is True
    assert cfg.persistence.cephfs.mountpoint == "/var/lib/docker/dnlab-backups"


def test_persistence_cephfs_config():
    raw = _base_raw()
    raw["infrastructure"]["persistence"] = {
        "backend": "cephfs",
        "root": "/mnt/dnlab-persist",
        "allow_migration_fallback": False,
        "cephfs": {
            "mountpoint": "/mnt/dnlab-persist",
            "expected_fstype": "ceph,fuseblk",
            "marker": ".shared",
        },
    }
    cfg = _parse_hosts_dict(raw)
    assert cfg.persistence.backend == "cephfs"
    assert cfg.persistence.root == "/mnt/dnlab-persist"
    assert cfg.persistence.allow_migration_fallback is False
    assert cfg.persistence.cephfs.mountpoint == "/mnt/dnlab-persist"
    assert cfg.persistence.cephfs.expected_fstype == "ceph,fuseblk"
    assert cfg.persistence.cephfs.marker == ".shared"


def test_persistence_backend_invalid():
    raw = _base_raw()
    raw["infrastructure"]["persistence"] = {"backend": "nfs"}
    with pytest.raises(HostsConfigError, match="persistence.backend"):
        _parse_hosts_dict(raw)


@pytest.mark.parametrize("bad", ["not-an-ip", "999.1.1.1", ""])
def test_ssh_bind_ip_invalid(bad):
    with pytest.raises(HostsConfigError, match="ssh_bind_ip"):
        _parse_hosts_dict(_base_raw(ssh_bind_ip=bad))


@pytest.mark.parametrize("bad", ["2200", "abc-def", "-2299", "2200-"])
def test_ssh_port_range_malformed(bad):
    with pytest.raises(HostsConfigError, match="ssh_port_range"):
        _parse_hosts_dict(_base_raw(ssh_port_range=bad))


@pytest.mark.parametrize("bad", ["0-100", "100-70000", "3000-2000"])
def test_ssh_port_range_out_of_bounds(bad):
    with pytest.raises(HostsConfigError, match="ssh_port_range"):
        _parse_hosts_dict(_base_raw(ssh_port_range=bad))
