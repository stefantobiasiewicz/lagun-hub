"""Configuration drivers for CAN hardware — setting it up, not moving frames.

One contract (``base``) for every piece of hardware a LagunBus bus runs through:

* ``usr_can315`` — the USR-CAN315 Ethernet↔CAN converter, configured with AT commands
  over the network (``eth-can0``, ``eth-can1``, …);
* ``socketcan`` — any CAN interface the kernel has (``can0``, ``can1``, …), whatever
  controller is behind it, configured with ``ip link``.

What they share is the question — "is this hardware set up the way the installation
needs it?" — not the way of answering it.
"""

from .base import (
    AdapterDriver,
    AdapterInfo,
    CanSettings,
    Change,
    DriverError,
    Mode,
    NotSupported,
    Rejected,
    Unreachable,
    configure,
)

__all__ = [
    "AdapterDriver",
    "AdapterInfo",
    "CanSettings",
    "Change",
    "DriverError",
    "Mode",
    "NotSupported",
    "Rejected",
    "Unreachable",
    "configure",
]
