"""A virtual bus — the same contract as SocketCAN, without a kernel and without
hardware.

It serves three purposes: working on macOS, testing the hub, and wiring the node
double ([`tests/node/`](../../../tests/node/)) to it.

**It fakes arbitration.** With several frames ready at once it lets the one with the
lower identifier through — without that, the priority tests would mean nothing.
"""

from __future__ import annotations

import contextlib
import heapq
import itertools
import socket
import threading
from collections import deque

from lagunhub.protocol.frame import Frame


class VirtualNetwork:
    """The cable the ports attach to.

    In ``manual=True`` mode nothing is delivered until ``flush()`` is called — which
    lets a test place several frames "at once" and check what order they come out in.
    """

    def __init__(self, *, manual: bool = False) -> None:
        self._manual = manual
        self._lock = threading.RLock()
        self._pending: list[tuple[int, int, VirtualBus, Frame]] = []
        self._ports: list[VirtualBus] = []
        self._seq = itertools.count()
        self._draining = False

    def attach(self, name: str) -> VirtualBus:
        port = VirtualBus(name, self)
        with self._lock:
            self._ports.append(port)
        return port

    def _detach(self, port: VirtualBus) -> None:
        with self._lock:
            if port in self._ports:
                self._ports.remove(port)

    def _offer(self, sender: VirtualBus, frame: Frame) -> None:
        with self._lock:
            heapq.heappush(self._pending, (frame.can_id, next(self._seq), sender, frame))
            if self._manual or self._draining:
                return
            self._drain_locked()

    def flush(self) -> None:
        """Deliver everything waiting, in arbitration order."""
        with self._lock:
            if not self._draining:
                self._drain_locked()

    def _drain_locked(self) -> None:
        self._draining = True
        try:
            # The loop re-reads the heap on every turn: a frame placed by another
            # thread mid-delivery joins the same arbitration queue instead of slipping
            # past it.
            while self._pending:
                _, _, sender, frame = heapq.heappop(self._pending)
                for port in self._ports:
                    if port is not sender:
                        port._deliver(frame)
        finally:
            self._draining = False


class VirtualBus:
    """A port on a ``VirtualNetwork``. Satisfies ``Bus``."""

    def __init__(self, name: str, network: VirtualNetwork) -> None:
        self.name = name
        self._network = network
        self._inbox: deque[Frame] = deque()
        self._lock = threading.Lock()
        # A socket pair gives a real descriptor, so select() behaves exactly as with
        # SocketCAN — and the RX thread need not know what it is sitting on.
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._closed = False

    def fileno(self) -> int:
        return self._wake_r.fileno()

    def _deliver(self, frame: Frame) -> None:
        with self._lock:
            if self._closed:
                return
            self._inbox.append(frame)
            # Wake-up buffer full? No harm — there is already something to wake on.
            with contextlib.suppress(BlockingIOError, OSError):
                self._wake_w.send(b"\x01")

    def recv(self) -> Frame | None:
        with self._lock:
            if not self._inbox:
                return None
            frame = self._inbox.popleft()
            with contextlib.suppress(BlockingIOError, OSError):
                self._wake_r.recv(1)
            return frame

    def send(self, frame: Frame) -> None:
        if self._closed:
            raise OSError("the bus is closed")
        self._network._offer(self, frame)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._network._detach(self)
        self._wake_r.close()
        self._wake_w.close()
