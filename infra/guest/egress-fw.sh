#!/bin/sh
# Egress firewall for the sales-bot test VM (runs inside the guest as root, idempotent).
# Goal: the guest and its containers reach the internet (DNS, apt, docker pull, OpenRouter, WhatsApp),
# but NOT the host, the LAN/router, Tailscale or link-local addresses.
# Order matters: established traffic and docker-bridge traffic are accepted BEFORE the private-range
# DROPs, otherwise container<->container traffic (172.x on br-*/docker0) would be dropped.
set -eu
IPT=iptables
PRIVATE="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16"

reset_chain() { $IPT -N "$1" 2>/dev/null || $IPT -F "$1"; }
add_drops() { for net in $PRIVATE; do $IPT -A "$1" -d "$net" -j DROP; done; $IPT -A "$1" -j RETURN; }

# 1) Traffic originated by the guest itself (OUTPUT).
OUT=SALESBOT-OUT
reset_chain $OUT
$IPT -A $OUT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
$IPT -A $OUT -o lo -j RETURN
# guest -> its own containers (published ports on 127.0.0.1, docker exec health checks, etc.)
$IPT -A $OUT -o docker0 -j RETURN
$IPT -A $OUT -o br-+ -j RETURN
# DHCP lease renewal with passt's built-in DHCP server
$IPT -A $OUT -p udp --dport 67 -j RETURN
add_drops $OUT
$IPT -C OUTPUT -j $OUT 2>/dev/null || $IPT -I OUTPUT 1 -j $OUT

# 2) Traffic forwarded for containers (DOCKER-USER, evaluated first in FORWARD by dockerd).
FWD=SALESBOT-FWD
reset_chain $FWD
$IPT -A $FWD -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
# container <-> container only when BOTH ends are docker bridges
$IPT -A $FWD -i br-+ -o br-+ -j RETURN
$IPT -A $FWD -i docker0 -o docker0 -j RETURN
add_drops $FWD
if $IPT -L DOCKER-USER -n >/dev/null 2>&1; then
  $IPT -C DOCKER-USER -j $FWD 2>/dev/null || $IPT -I DOCKER-USER 1 -j $FWD
else
  echo "egress-fw: DOCKER-USER missing (dockerd not started yet?)" >&2
fi
# Drop the chain name used by an earlier version of this script, if present.
for c in OUTPUT DOCKER-USER; do $IPT -D "$c" -j SALESBOT-EGRESS 2>/dev/null || true; done
$IPT -F SALESBOT-EGRESS 2>/dev/null && $IPT -X SALESBOT-EGRESS 2>/dev/null || true
echo "egress-fw: applied"
