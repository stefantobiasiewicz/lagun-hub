#!/usr/bin/env bash
# Bringing a CAN interface up. Handles both a hardware one and a virtual one.
#
# The speed is set not by the hub but by the kernel — and this is the command that
# does it. Split out into a script because `vcan` and `can` come up differently, and
# retyping this from memory after every RPi restart ends in a typo.
#
#   ./can-up.sh can0 500000     hardware, with timing
#   ./can-up.sh vcan0           virtual, no timing
set -euo pipefail

INTERFACE="${1:?usage: can-up.sh <interface> [bitrate]}"
BITRATE="${2:-500000}"

if [[ "$INTERFACE" == vcan* ]]; then
    # A virtual one has to be created first — it is not in the hardware.
    modprobe vcan
    ip link show "$INTERFACE" >/dev/null 2>&1 || ip link add dev "$INTERFACE" type vcan
    ip link set up "$INTERFACE"
    echo "$INTERFACE: virtual, ready"
    exit 0
fi

if ! ip link show "$INTERFACE" >/dev/null 2>&1; then
    echo "there is no interface $INTERFACE" >&2
    echo "  the driver did not come up, or the device tree overlay is not configured" >&2
    echo "  check: dmesg | grep -i mcp" >&2
    exit 1
fi

# Changing the timing needs the interface down. `|| true`, because it may already be.
ip link set "$INTERFACE" down 2>/dev/null || true

# NO `fd on`: the MCP2518FD can do CAN FD and would gladly use it, but the LagunBus
# specification says CAN 2.0B. An FD frame on this bus is a frame nobody will
# understand.
ip link set "$INTERFACE" up type can bitrate "$BITRATE" restart-ms 100

echo "$INTERFACE: $((BITRATE / 1000)) kbit/s, ready"
ip -details -brief link show "$INTERFACE"
