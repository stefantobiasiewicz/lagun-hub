"""The bus interface.

The hub is developed on macOS, where SocketCAN does not exist, and runs on Linux.
That is why the transport is an interface from day one rather than after the first
pain.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from lagunhub.protocol.frame import Frame


class TransmitQueueFull(OSError):
    """The kernel send queue is full.

    The fast path then drops the frame and bumps a counter — a late command is worse
    than a missing one. CORE hands the request to its own timer, because a retry is
    part of the transaction anyway.
    """


@runtime_checkable
class Bus(Protocol):
    """One bus. Non-blocking, with a descriptor fit for ``select()``."""

    name: str

    def fileno(self) -> int:
        """A descriptor for ``select()``. Ready to read => ``recv()`` will return something."""
        ...

    def recv(self) -> Frame | None:
        """One frame, or ``None`` when nothing is waiting. It never blocks.

        ``OSError`` when the bus failed (``ENETDOWN`` once as the interface goes down,
        ``ENODEV`` when it disappears) — RX counts it and moves on to the other buses."""
        ...

    def send(self, frame: Frame) -> None:
        """Send a frame. Raises ``TransmitQueueFull`` when the kernel will not take it,
        and another ``OSError`` when the bus is not there to take it (``ENETDOWN`` while
        the interface is down) — the hub counts both as a frame that did not go out."""
        ...

    def close(self) -> None: ...
