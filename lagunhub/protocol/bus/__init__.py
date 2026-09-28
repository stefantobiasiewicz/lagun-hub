"""The bus layer: one interface, three implementations."""

from lagunhub.protocol.bus.base import Bus, TransmitQueueFull
from lagunhub.protocol.bus.socketcan import HAS_SOCKETCAN, SocketCanBus
from lagunhub.protocol.bus.virtual import VirtualBus, VirtualNetwork

__all__ = [
    "HAS_SOCKETCAN",
    "Bus",
    "SocketCanBus",
    "TransmitQueueFull",
    "VirtualBus",
    "VirtualNetwork",
]
