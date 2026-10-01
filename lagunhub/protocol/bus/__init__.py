"""The bus layer: one interface, three implementations — SocketCAN for a house, the
virtual bus for tests, UDP multicast for running with simulated nodes."""

from lagunhub.protocol.bus.base import Bus, TransmitQueueFull
from lagunhub.protocol.bus.socketcan import HAS_SOCKETCAN, SocketCanBus
from lagunhub.protocol.bus.udp import UdpBus
from lagunhub.protocol.bus.virtual import VirtualBus, VirtualNetwork

__all__ = [
    "HAS_SOCKETCAN",
    "Bus",
    "SocketCanBus",
    "TransmitQueueFull",
    "UdpBus",
    "VirtualBus",
    "VirtualNetwork",
]
