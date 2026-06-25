#!/usr/bin/env bash
# Optional packet-loss injection for adversarial quorum-failure tests.
# Run on the machine that routes testbed traffic (often the Mac host or AP).
#
# Usage:
#   sudo ./benchmark/test_scripts/netem_packet_loss.sh en0 5 on
#   sudo ./benchmark/test_scripts/netem_packet_loss.sh en0 0 off
#
# Find interface: route -n get 192.168.10.1 | grep interface

set -euo pipefail

IFACE="${1:?interface required, e.g. en0}"
LOSS="${2:-5}"
ACTION="${3:-on}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

if [[ "$ACTION" == "off" ]]; then
  tc qdisc del dev "$IFACE" root netem 2>/dev/null || true
  echo "[netem] Cleared netem on $IFACE"
  exit 0
fi

tc qdisc del dev "$IFACE" root netem 2>/dev/null || true
tc qdisc add dev "$IFACE" root netem loss "${LOSS}%"
echo "[netem] Applied ${LOSS}% loss on $IFACE (MeshDNS UDP may share this path)"
