import pytest

from dnlab_multinode.services.mgmt_ips import (
    MgmtAddressError, derive_ipv6_subnet_from_ipv4, ipv4_reservations,
    ipv6_gateway,
)


def test_ipv4_reservations_slash24():
    r = ipv4_reservations("172.20.20.0/24")
    assert r.docker_gw == "172.20.20.1"
    assert r.anchor == "172.20.20.252"
    assert r.dns == "172.20.20.253"
    assert r.jumphost == "172.20.20.254"


def test_ipv4_reservations_slash29():
    r = ipv4_reservations("10.0.0.0/29")
    assert r.docker_gw == "10.0.0.1"
    assert r.anchor == "10.0.0.4"
    assert r.dns == "10.0.0.5"
    assert r.jumphost == "10.0.0.6"


def test_ipv4_reservations_too_small():
    with pytest.raises(MgmtAddressError):
        ipv4_reservations("10.0.0.0/30")


def test_ipv6_gateway_last_address():
    assert ipv6_gateway("2001:db8:20::/120") == "2001:db8:20::ff"


def test_derived_ipv6_subnet_is_not_ipv4_mapped():
    subnet = derive_ipv6_subnet_from_ipv4("172.20.21.0/24")
    assert subnet == "3fff:172:20:21::/64"
    assert not subnet.startswith("::ffff:")
