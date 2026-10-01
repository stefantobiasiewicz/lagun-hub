"""A bus over UDP multicast — for running the hub with simulated nodes, not for a house.

**Test and development only.** It exists so that the hub and the simulator
(`lagun-sim`, zadania/zrobione/FEAT-02.md) can run as separate processes on a machine without
CAN — macOS above all, where there is no SocketCAN at all. The hub reaches it only
through `lagun-hub run --udp`; it is deliberately **not** a kind of bus in `hub.yaml`,
so that a house configuration cannot point at it by mistake.

Why multicast (FEAT-02, N1): every process joins one group and sees every other
process's frames — a cable, with no broker to start first and no order to start in.
A Unix socket cannot multicast; it would need a broker process. The price: there is
**no arbitration between processes** — frames arrive in the order they were sent, not
by identifier. The arbitration tests run on `VirtualBus`, which does fake it.

The datagram, 24 bytes. The node side (`lagunbus_node`, a separate implementation
sharing no code with this one) writes and reads the same; the format is described once,
in `lagunbus-node/README.md` § Decisions, and `tests/test_udp_bus.py` pins it with a
byte vector:

    offset  size
    0       4     magic  b"LBU1"   — LagunBus over UDP, version 1
    4       4     sender           — random u32 per open bus, little-endian
    8       16    struct can_frame — the SocketCAN layout, as in `socketcan.py`

A process receives its own datagrams back from the group; `sender` is how it drops
them, the way SocketCAN does not return a socket's own frames.
"""

from __future__ import annotations

import errno
import secrets
import socket
import struct
from typing import Final

from lagunhub.protocol.bus.base import TransmitQueueFull
from lagunhub.protocol.bus.socketcan import CAN_EFF_FLAG, FRAME_STRUCT
from lagunhub.protocol.frame import Frame

DEFAULT_GROUP: Final = "239.255.76.66"
"""Administratively scoped (239/8) — never routed off the site."""

DEFAULT_PORT: Final = 7607

MAGIC: Final = b"LBU1"
HEADER: Final = struct.Struct("<4sI")
DATAGRAM_SIZE: Final = HEADER.size + FRAME_STRUCT.size  # 24

LOOPBACK: Final = "127.0.0.1"


def parse_address(text: str) -> tuple[str, int]:
    """`GROUP[:PORT]` → `(group, port)`. Raises `ValueError` on anything else."""
    group, _, port = text.partition(":")
    group = group or DEFAULT_GROUP
    try:
        packed = socket.inet_aton(group)
    except OSError:
        raise ValueError(f"{group!r} is not an IPv4 address") from None
    if not 224 <= packed[0] <= 239:
        raise ValueError(f"{group} is not a multicast address (224.0.0.0–239.255.255.255)")
    number = int(port) if port else DEFAULT_PORT
    if not 0 < number < 65536:
        raise ValueError(f"port {number} out of range")
    return group, number


def encode(sender: int, frame: Frame) -> bytes:
    return HEADER.pack(MAGIC, sender) + FRAME_STRUCT.pack(
        frame.can_id | CAN_EFF_FLAG, len(frame.data), frame.data.ljust(8, b"\x00")
    )


def decode(datagram: bytes) -> tuple[int, Frame] | None:
    """`(sender, frame)`, or `None` for anything that is not our datagram — the group
    is open to any program on the machine, so garbage is dropped, not fatal."""
    if len(datagram) != DATAGRAM_SIZE:
        return None
    magic, sender = HEADER.unpack_from(datagram)
    if magic != MAGIC:
        return None
    can_id, length, payload = FRAME_STRUCT.unpack_from(datagram, HEADER.size)
    if length > 8:
        return None
    return sender, Frame(can_id & ~CAN_EFF_FLAG, payload[:length])


class UdpBus:
    """One port on the multicast group. Satisfies ``Bus``.

    Two sockets, because macOS refuses to SEND from a socket bound to a group address
    (`EADDRNOTAVAIL`), and binding to the group is what keeps unrelated unicast traffic
    on the port out. Both are pinned to the loopback interface: the frames stay on this
    machine.
    """

    def __init__(
        self, group: str = DEFAULT_GROUP, port: int = DEFAULT_PORT, *, name: str = "udp"
    ) -> None:
        self.name = name
        self.address = (group, port)
        self.sender = secrets.randbits(32)
        loopback = socket.inet_aton(LOOPBACK)

        self._rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            self._rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):  # macOS needs it for a second listener
                self._rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            self._rx.bind((group, port))
            self._rx.setsockopt(
                socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group) + loopback
            )
            self._rx.setblocking(False)

            self._tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        except OSError:
            self._rx.close()
            raise
        try:
            self._tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, loopback)
            self._tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            self._tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
            self._tx.setblocking(False)
        except OSError:
            self._rx.close()
            self._tx.close()
            raise

    def fileno(self) -> int:
        return self._rx.fileno()

    def recv(self) -> Frame | None:
        while True:
            try:
                datagram = self._rx.recv(64)
            except BlockingIOError:
                return None
            decoded = decode(datagram)
            if decoded is None or decoded[0] == self.sender:
                continue  # not ours, or our own frame coming back from the group
            return decoded[1]

    def send(self, frame: Frame) -> None:
        try:
            self._tx.sendto(encode(self.sender, frame), self.address)
        except OSError as exc:
            if exc.errno in (errno.ENOBUFS, errno.EAGAIN):
                raise TransmitQueueFull(exc.errno, "send queue full") from exc
            raise

    def close(self) -> None:
        self._rx.close()
        self._tx.close()
