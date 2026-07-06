#!/bin/bash
set -e

# Space-separated list of upstream DNS servers
UPSTREAM="${UPSTREAM_DNS:-1.1.1.1}"

mkdir -p /etc/dnlab-dns
# Don't touch the file if it's already there — it's bind-mounted read-only
# from the host. Only create an empty one as a fallback for manual runs.
[ -e /etc/dnlab-dns/hosts ] || touch /etc/dnlab-dns/hosts

{
    echo "no-resolv"
    echo "no-hosts"
    echo "addn-hosts=/etc/dnlab-dns/hosts"
    echo "domain-needed"
    echo "bogus-priv"
    echo "log-queries"
    for s in $UPSTREAM; do
        echo "server=$s"
    done
} > /etc/dnsmasq.conf

echo "dnsmasq starting — upstream: $UPSTREAM"
exec dnsmasq -k --conf-file=/etc/dnsmasq.conf
