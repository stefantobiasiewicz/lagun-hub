"""A bus over a raw SocketCAN socket.

No external dependencies: ``AF_CAN``/``SOCK_RAW`` plus ``struct``. The module
**imports everywhere**, macOS included — only creating the object needs Linux. That
way the package can be imported on the machine it is written on.
"""

from __future__ import annotations

import errno
import socket
import struct
from typing import Final

from lagunhub.protocol.bus.base import TransmitQueueFull
from lagunhub.protocol.frame import Frame

CAN_EFF_FLAG: Final = 0x8000_0000
"""Extended identifier. LagunBus uses nothing else."""

CAN_RTR_FLAG: Final = 0x4000_0000
"""Remote transmission request — LagunBus has none (zadania/BUG-10.md)."""

CAN_ERR_FLAG: Final = 0x2000_0000
"""An error frame from the controller — only with an error mask set, which the hub does
not set; refused all the same."""

CAN_EFF_MASK: Final = 0x1FFF_FFFF

# From `linux/can/raw.h`, fetched like the two above; the fallbacks are the kernel's
# numbers, used only where the socket could not be created anyway.
_SOL_CAN_RAW: int = getattr(socket, "SOL_CAN_RAW", 101)
_CAN_RAW_FILTER: int = getattr(socket, "CAN_RAW_FILTER", 1)

_EXTENDED_ONLY: Final = struct.pack("=II", CAN_EFF_FLAG, CAN_EFF_FLAG | CAN_RTR_FLAG)
"""One `can_filter`: the EFF bit set and the RTR bit clear, any identifier. The kernel
then drops a standard or remote frame before it reaches the hub's queue."""

FRAME_STRUCT: Final = struct.Struct("<IB3x8s")
FRAME_SIZE: Final = FRAME_STRUCT.size  # 16

# These constants exist only on Linux. Fetching them through getattr means the module
# type-checks and imports on every platform — and their absence surfaces only when a
# socket is actually created, with a readable message.
_AF_CAN: int = getattr(socket, "AF_CAN", -1)
_CAN_RAW: int = getattr(socket, "CAN_RAW", -1)

HAS_SOCKETCAN: Final = _AF_CAN != -1 and _CAN_RAW != -1


class SocketCanBus:
    """One CAN bus. Satisfies ``Bus``.

    **The socket can be replaced under a running hub** (zadania/zrobione/BUG-04.md, D3). A
    CAN_RAW socket is bound to the interface's *index*: it survives the interface going
    ``down`` and ``up``, but not the interface going away — a USB adapter pulled out and
    plugged back gets a new index, and the old socket hears nothing on it. RX, the
    binding engine and CORE each hold this object, not the socket, so `reopen` swaps the
    socket in place and nobody has to be told — RX re-registers the new descriptor with
    its ``select`` when it sees `fileno` change.
    """

    def __init__(self, channel: str, *, name: str | None = None, missing_ok: bool = False) -> None:
        """``missing_ok``: an interface the kernel does not have yet gives an unbound
        socket instead of ``OSError`` — it hears nothing and every send fails, until
        `reopen` finds the interface. The hub's start uses it, so a bus whose adapter is
        not plugged in yet is still the hub's bus, watched and opened when it comes."""
        if not HAS_SOCKETCAN:
            raise RuntimeError(
                "SocketCAN is available on Linux only; on this machine use VirtualBus"
            )
        self.name = name or channel
        self.channel = channel
        self.ifindex = 0
        """The interface index the socket is bound to; 0 while unbound."""
        try:
            self._sock = self._bound()
        except OSError as exc:
            if not missing_ok or exc.errno != errno.ENODEV:
                raise
            self._sock = socket.socket(_AF_CAN, socket.SOCK_RAW, _CAN_RAW)
            self._sock.setblocking(False)

    def _bound(self) -> socket.socket:
        sock = socket.socket(_AF_CAN, socket.SOCK_RAW, _CAN_RAW)
        try:
            sock.setsockopt(_SOL_CAN_RAW, _CAN_RAW_FILTER, _EXTENDED_ONLY)
            sock.bind((self.channel,))
            self.ifindex = socket.if_nametoindex(self.channel)
        except OSError:
            sock.close()
            raise
        sock.setblocking(False)
        return sock

    def reopen(self) -> None:
        """A new socket on the interface as it is now; the old one is closed after.

        Raises ``OSError`` (``ENODEV``) when the interface is still not there — the old
        socket then stays as it was.
        """
        sock = self._bound()
        old, self._sock = self._sock, sock
        old.close()

    def fileno(self) -> int:
        return self._sock.fileno()

    def recv(self) -> Frame | None:
        """The next LagunBus frame; one that is not is skipped, not returned as `None`
        — `None` means "nothing waiting", and RX would stop draining on it."""
        while True:
            try:
                raw = self._sock.recv(FRAME_SIZE)
            except BlockingIOError:
                return None
            frame = unpack(raw)
            if frame is not None:
                return frame

    def send(self, frame: Frame) -> None:
        raw = FRAME_STRUCT.pack(
            frame.can_id | CAN_EFF_FLAG,
            len(frame.data),
            frame.data.ljust(8, b"\x00"),
        )
        try:
            self._sock.send(raw)
        except OSError as exc:
            if exc.errno in (errno.ENOBUFS, errno.EAGAIN):
                raise TransmitQueueFull(exc.errno, "send queue full") from exc
            raise

    def close(self) -> None:
        self._sock.close()


def unpack(raw: bytes) -> Frame | None:
    """A kernel `can_frame`, or `None` when it is not a LagunBus frame.

    The filter on the socket already keeps standard and remote frames out; this check is
    what holds without it — before it, an RTR frame kept its flag in the identifier,
    `Frame` refused 30 bits with `ValueError`, and that ended the RX thread
    (zadania/BUG-10.md). A standard 11-bit frame was worse: read quietly as a LagunBus
    identifier.
    """
    if len(raw) < FRAME_SIZE:
        return None
    can_id, length, payload = FRAME_STRUCT.unpack(raw)
    if not can_id & CAN_EFF_FLAG or can_id & (CAN_RTR_FLAG | CAN_ERR_FLAG):
        return None
    return Frame(can_id & CAN_EFF_MASK, payload[: min(length, 8)])
