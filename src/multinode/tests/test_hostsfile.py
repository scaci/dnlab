"""Tests for the /etc/hosts parser and merge logic."""

from unittest.mock import MagicMock

from dnlab_multinode.services.hostsfile import (
    HostEntry, parse_hosts_block, collect_hosts_entries,
    render_hosts_file, get_upstream_dns,
)


_HOSTS_SAMPLE = """\
127.0.0.1       localhost
10.0.0.10       c-lab-01

###### CLAB-test-vrf-START ######
192.168.200.12  clab-test-vrf-NX2 10524bb6b662  # Kind: cisco_n9kv
192.168.200.254 clab-test-vrf-tor 0c1021f1efa4  # Kind: juniper_vjunosswitch
192.168.200.11  clab-test-vrf-NX1 08803f05f416  # Kind: cisco_n9kv
###### CLAB-test-vrf-END ######

###### CLAB-other-lab-START ######
10.99.0.1       clab-other-lab-R1 dead beef  # Kind: linux
###### CLAB-other-lab-END ######
"""


def test_parse_hosts_block_basic():
    entries = parse_hosts_block(_HOSTS_SAMPLE, "test-vrf")
    names = {e.name for e in entries}
    assert names == {
        "clab-test-vrf-NX1",
        "clab-test-vrf-NX2",
        "clab-test-vrf-tor",
    }


def test_parse_hosts_block_isolates_lab():
    """Entries of other labs must not leak into the zone."""
    entries = parse_hosts_block(_HOSTS_SAMPLE, "test-vrf")
    for e in entries:
        assert not e.name.startswith("clab-other-lab")


def test_parse_hosts_block_ignores_hash():
    """The container short hash (3rd column) must not become a DNS name."""
    entries = parse_hosts_block(_HOSTS_SAMPLE, "test-vrf")
    hashes = {"10524bb6b662", "0c1021f1efa4", "08803f05f416"}
    names = {e.name for e in entries}
    assert not (hashes & names)


def test_parse_hosts_block_ignores_kind_comment():
    entries = parse_hosts_block(_HOSTS_SAMPLE, "test-vrf")
    # None of the entries should contain '#' or 'Kind' literals
    for e in entries:
        assert "#" not in e.name
        assert "Kind" not in e.name


def test_parse_hosts_block_correct_ips():
    entries = parse_hosts_block(_HOSTS_SAMPLE, "test-vrf")
    by_name = {e.name: e.ip for e in entries}
    assert by_name["clab-test-vrf-NX1"] == "192.168.200.11"
    assert by_name["clab-test-vrf-NX2"] == "192.168.200.12"
    assert by_name["clab-test-vrf-tor"] == "192.168.200.254"


def test_parse_hosts_ipv4_and_ipv6():
    sample = """\
###### CLAB-v6-START ######
2001:db8::11    clab-v6-R1 aaaa  # Kind: linux
192.168.50.11   clab-v6-R2 bbbb  # Kind: linux
###### CLAB-v6-END ######
"""
    entries = parse_hosts_block(sample, "v6")
    by_name = {e.name: e for e in entries}
    assert by_name["clab-v6-R1"].family == "AAAA"
    assert by_name["clab-v6-R2"].family == "A"


def test_parse_empty_block():
    sample = """\
###### CLAB-empty-START ######
###### CLAB-empty-END ######
"""
    assert parse_hosts_block(sample, "empty") == []


def test_parse_no_block_for_lab():
    assert parse_hosts_block(_HOSTS_SAMPLE, "nonexistent") == []


def test_parse_hosts_invalid_ip_skipped():
    sample = """\
###### CLAB-bad-START ######
not-an-ip  clab-bad-R1  abc
10.0.0.1   clab-bad-R2  def
###### CLAB-bad-END ######
"""
    entries = parse_hosts_block(sample, "bad")
    assert len(entries) == 1
    assert entries[0].name == "clab-bad-R2"


def test_collect_merges_master_first():
    """Master entries must take precedence over worker entries."""
    master_hosts = """\
###### CLAB-lab-START ######
192.168.200.11  clab-lab-R1 aaa  # Kind: linux
###### CLAB-lab-END ######
"""
    worker_hosts = """\
###### CLAB-lab-START ######
192.168.200.12  clab-lab-R2 bbb  # Kind: linux
###### CLAB-lab-END ######
"""

    master = MagicMock()
    master.run.return_value = master_hosts
    worker = MagicMock()
    worker.run.return_value = worker_hosts

    clients = {"master": master, "worker1": worker}
    entries = collect_hosts_entries("lab", clients)

    names = {e.name for e in entries}
    assert names == {"clab-lab-R1", "clab-lab-R2"}


def test_collect_dedup_keeps_first(caplog):
    """Duplicate name with different IPs → warning + first wins."""
    master_hosts = """\
###### CLAB-lab-START ######
192.168.200.11  clab-lab-R1 aaa  # Kind: linux
###### CLAB-lab-END ######
"""
    worker_hosts = """\
###### CLAB-lab-START ######
192.168.200.99  clab-lab-R1 zzz  # Kind: linux
###### CLAB-lab-END ######
"""
    master = MagicMock()
    master.run.return_value = master_hosts
    worker = MagicMock()
    worker.run.return_value = worker_hosts

    with caplog.at_level("WARNING"):
        entries = collect_hosts_entries(
            "lab", {"master": master, "worker1": worker},
        )

    assert len(entries) == 1
    assert entries[0].ip == "192.168.200.11"  # master wins
    assert any("Duplicate name" in r.message for r in caplog.records)


def test_render_hosts_file():
    entries = [
        HostEntry(name="clab-lab-R2", ip="192.168.200.12", family="A"),
        HostEntry(name="clab-lab-R1", ip="192.168.200.11", family="A"),
    ]
    text = render_hosts_file(entries)
    # Both entries present
    assert "clab-lab-R1" in text
    assert "clab-lab-R2" in text
    assert "192.168.200.11" in text
    assert "192.168.200.12" in text


def test_upstream_from_resolvconf():
    client = MagicMock()
    client.run.return_value = (
        "nameserver 192.168.1.1\n"
        "nameserver 8.8.8.8\n"
    )
    servers = get_upstream_dns(client)
    assert servers == ["192.168.1.1", "8.8.8.8"]


def test_upstream_skips_loopback_stub():
    """systemd-resolved stub 127.0.0.53 must be filtered out."""
    client = MagicMock()
    client.run.return_value = "nameserver 127.0.0.53\n"
    servers = get_upstream_dns(client)
    # Fallback since the only nameserver was loopback
    assert servers == ["1.1.1.1"]


def test_upstream_fallback_on_empty():
    client = MagicMock()
    client.run.return_value = ""
    servers = get_upstream_dns(client)
    assert servers == ["1.1.1.1"]


def test_upstream_ignores_garbage():
    client = MagicMock()
    client.run.return_value = (
        "nameserver not-an-ip\n"
        "nameserver 192.168.1.1\n"
        "search example.local\n"
    )
    servers = get_upstream_dns(client)
    assert servers == ["192.168.1.1"]
