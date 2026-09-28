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

FRAME_STRUCT: Final = struct.Struct("<IB3x8s")
FRAME_SIZE: Final = FRAME_STRUCT.size  # 16

# These constants exist only on Linux. Fetching them through getattr means the module
# type-checks and imports on every platform — and their absence surfaces only when a
# socket is actually created, with a readable message.
_AF_CAN: int = getattr(socket, "AF_CAN", -1)
_CAN_RAW: int = getattr(socket, "CAN_RAW", -1)

HAS_SOCKETCAN: Final = _AF_CAN != -1 and _CAN_RAW != -1


class SocketCanBus:
    """One CAN bus. Satisfies ``Bus``."""

    def __init__(self, channel: str, *, name: str | None = None) -> None:
        if not HAS_SOCKETCAN:
            raise RuntimeError(
                "SocketCAN is available on Linux only; on this machine use VirtualBus"
            )
        self.name = name or channel
        self.channel = channel
        self._sock = socket.socket(_AF_CAN, socket.SOCK_RAW, _CAN_RAW)
        self._sock.bind((channel,))
        self._sock.setblocking(False)

    def fileno(self) -> int:
        return self._sock.fileno()

    def recv(self) -> Frame | None:
        try:
            raw = self._sock.recv(FRAME_SIZE)
        except BlockingIOError:
            return None
        if len(raw) < FRAME_SIZE:
            return None
        can_id, length, payload = FRAME_STRUCT.unpack(raw)
        return Frame(can_id & ~CAN_EFF_FLAG, payload[:length])

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
