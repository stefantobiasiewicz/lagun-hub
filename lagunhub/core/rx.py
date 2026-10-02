"""The RX thread — the only one that reads from the buses.

It blocks on ``select()`` over every socket at once and **waits on nothing but the
bus**. A frame with a time budget it handles at once; all the rest it puts in a queue
and forgets about.

The queue is filled with ``put_nowait``, never ``put``: a full queue means the consumer
has stalled, and RX has no right to stall with it.
"""

from __future__ import annotations

import queue
import selectors
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from lagunhub.core.bindings import BindingEngine
from lagunhub.core.busload import BusCounts
from lagunhub.protocol.bus.base import Bus
from lagunhub.protocol.frame import Frame


@dataclass(frozen=True, slots=True)
class Received:
    """A frame received from the bus."""

    bus: int
    frame: Frame


@dataclass(frozen=True, slots=True)
class Sent:
    """A frame the hub sent itself.

    The slow path has to know about it for two reasons. First, a command sent as a
    binding’s effect is a **transaction** and somebody has to mind its retries — and
    that is CORE, not the RX thread ([`ARCHITEKTURA.md`](../ARCHITEKTURA.md) §4.2).

    Second, on real SocketCAN **a socket does not receive its own frames**. A watch fed
    only by reception would show an event and a reply while swallowing the command
    between them — lying about exactly what the hub did. On a virtual bus this would not
    show, because there the watch sits on a separate port.
    """

    bus: int
    frame: Frame
    at: float


@dataclass(frozen=True, slots=True)
class Task:
    """A command for CORE placed from outside its thread.

    The model belongs to CORE alone, so an adapter wanting to change something or ask
    something **puts it in the queue** instead of reaching in directly. `lagunhub.api` adds a
    `Future` and a named list of operations on top; here there is only the route.
    """

    action: Callable[[], None]


ColdItem = Received | Sent | Task


class RxThread(threading.Thread):
    def __init__(
        self,
        buses: Sequence[Bus],
        engine: BindingEngine,
        cold: queue.Queue[ColdItem] | None = None,
        *,
        name: str = "rx",
    ) -> None:
        super().__init__(name=name, daemon=True)
        self.buses = list(buses)
        self.engine = engine
        self.cold: queue.Queue[ColdItem] = cold or queue.Queue(maxsize=4096)
        self.received = 0
        self.counts = BusCounts(len(self.buses))
        """Received frames per bus — counted here, before the slow queue that may
        overflow (`core/busload.py`)."""
        self.overflowed = 0
        self._stop = threading.Event()

    def pump(self) -> int:
        """Handle everything waiting and return. Tests call this instead of start().

        That way there is not a single sleep() in them, and the order of events is
        exactly what the test sets.
        """
        handled = 0
        for index, bus in enumerate(self.buses):
            while (frame := bus.recv()) is not None:
                self._dispatch(index, frame)
                handled += 1
        return handled

    def _dispatch(self, index: int, frame: Frame) -> None:
        self.received += 1
        self.counts.count(index, frame)
        self.engine.handle(frame)  # the fast path — first, always
        self.offer(Received(bus=index, frame=frame))

    def offer(self, item: ColdItem) -> None:
        """Into the slow queue. `put_nowait`, never `put` — RX has no right to stall."""
        try:
            self.cold.put_nowait(item)
        except queue.Full:
            self.overflowed += 1

    def run(self) -> None:
        selector = selectors.DefaultSelector()
        for index, bus in enumerate(self.buses):
            selector.register(bus, selectors.EVENT_READ, index)
        try:
            while not self._stop.is_set():
                for key, _ in selector.select(timeout=0.2):
                    bus = self.buses[key.data]
                    while (frame := bus.recv()) is not None:
                        self._dispatch(key.data, frame)
        finally:
            selector.close()

    def stop(self) -> None:
        self._stop.set()
