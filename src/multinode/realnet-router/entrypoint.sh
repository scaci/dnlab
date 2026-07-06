#!/bin/sh
set -eu

REALNET_IPV4="${REALNET_IPV4:-}"
NAT_ENABLED="${NAT_ENABLED:-true}"
OSPF_ENABLED="${OSPF_ENABLED:-false}"
OSPF_EXTERNAL_IFACE="${OSPF_EXTERNAL_IFACE:-}"

WAN_IF="eth0"
LAN_IF="eth1"

echo "[realnet-router] starting wan=${WAN_IF} lan=${LAN_IF} realnet=${REALNET_IPV4} nat=${NAT_ENABLED} ospf=${OSPF_ENABLED}"

sysctl -w net.ipv4.ip_forward=1 >/dev/null

while ! ip link show "$LAN_IF" >/dev/null 2>&1; do
  echo "[realnet-router] waiting for ${LAN_IF}"
  sleep 1
done

ip link set "$LAN_IF" up
if [ -n "$REALNET_IPV4" ]; then
  ip addr replace "$REALNET_IPV4" dev "$LAN_IF"
fi

if [ "$OSPF_ENABLED" = "true" ]; then
  WAN_IP="$(ip -4 -o addr show dev "$WAN_IF" | awk '{print $4}' | cut -d/ -f1 | head -n1)"
  LAN_CIDR="$(ip -4 -o addr show dev "$LAN_IF" | awk '{print $4}' | head -n1)"
  LAN_IP="$(printf '%s\n' "$LAN_CIDR" | cut -d/ -f1)"
  AREA="${LAN_IP:-0.0.0.1}"
  WAN_CIDR="$(ip -4 -o addr show dev "$WAN_IF" | awk '{print $4}' | head -n1)"

  sed -i 's/^ospfd=no/ospfd=yes/' /etc/frr/daemons
  cat >/etc/frr/frr.conf <<EOF
frr defaults traditional
hostname realnet-router
service integrated-vtysh-config
!
router ospf
 ospf router-id ${WAN_IP:-1.1.1.1}
 default-information originate
 network ${WAN_CIDR:-0.0.0.0/0} area 0.0.0.0
 network ${LAN_CIDR:-0.0.0.0/0} area ${AREA}
!
line vty
EOF
  chown frr:frr /etc/frr/frr.conf
  echo "[realnet-router] OSPF enabled, real_net area=${AREA}"
  /usr/lib/frr/frrinit.sh start
else
  if [ "$NAT_ENABLED" = "true" ]; then
    iptables -t nat -C POSTROUTING -o "$WAN_IF" -j MASQUERADE 2>/dev/null \
      || iptables -t nat -A POSTROUTING -o "$WAN_IF" -j MASQUERADE
    echo "[realnet-router] NAT enabled ${LAN_IF} -> ${WAN_IF}"
  fi
fi

tail -f /dev/null
