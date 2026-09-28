"""The hub’s event stream — one source for SSE, for MQTT and for the view.

An adapter has no right to poll the hub in a loop to see whether anything changed: at
forty nodes that is forty times more work than saying once that one thing changed. So
CORE **announces** and the adapters listen.

Two things have to be right here, or the slowest listener becomes the hub’s
bottleneck:

* **each listener’s queue is its own and bounded** — a slow client loses at its own end,
  not at the sender’s,
* **announcing never blocks** — a full queue drops the oldest and bumps a counter, the
  same as the traffic watch ([`ARCHITEKTURA.md`](../ARCHITEKTURA.md) §10.3.3).

This is the second thing after `Snapshot` that CORE lets out — and the only one that
goes to the adapters. The model stays with CORE ([`api/`](../api/)).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from types import TracebackType
from typing import Final

from lagunhub.core.model import Uid
from lagunhub.protocol.values import Value

DEPTH: Final = 512
"""How many events one listener may have pending before it starts losing the
oldest."""


class EventKind(Enum):
    STATE = "state"
    """An attribute **changed** value. A report carrying the same thing as before is not
    an event and does not go out (07-hub.md §3.1)."""

    EVENT = "event"
    """An `EVENT` or an `ALARM` from a node — a click, a threshold crossed, a restart."""

    NODE = "node"
    """A change in the installation’s make-up: a node joined, was identified, fell
    silent."""

    ISSUE = "issue"
    """Something needs an operator decision. The hub does not guess at it, it shows it
    (07-hub.md §5.3)."""


@dataclass(frozen=True, slots=True)
class Event:
    """What happened. The names are already expanded, because only CORE has what it takes
    to expand them."""

    kind: EventKind
    at: float
    uid: Uid | None = None
    endpoint: int = 0
    element: int = 0
    name: str = ""
    """`OnOff`, `Click`, `READY` — depending on the kind."""

    value: Value | None = None
    raw: bytes = b""
    text: str = ""
    """A sentence for a person. Filled in only where there is no other way: issues."""


class Subscription[T]:
    """One listener. Closing is mandatory — otherwise CORE announces into the void."""

    __slots__ = ("_bus", "_queue", "dropped")

    def __init__(self, bus: Bus[T], depth: int) -> None:
        self._bus = bus
        self._queue: queue.Queue[T | None] = queue.Queue(maxsize=depth)
        self.dropped = 0

    def offer(self, event: T) -> None:
        """Called from the CORE thread. Never blocks and never raises."""
        try:
            self._queue.put_nowait(event)
            return
        except queue.Full:
            self.dropped += 1
        with suppress(queue.Empty):
            self._queue.get_nowait()  # the oldest one goes overboard
        with suppress(queue.Full):
            self._queue.put_nowait(event)

    def get(self, timeout: float) -> T | None:
        """The next event, or `None` when nothing arrived or the subscription is gone."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def closed(self) -> bool:
        """Whether the subscription is already detached. Silence differs from an ending —
        an SSE stream has to tell the two apart to know when to send a heartbeat."""
        return self not in self._bus.listeners

    def close(self) -> None:
        """Unhooks from the bus and wakes whoever is waiting on `get`."""
        self._bus.unsubscribe(self)
        with suppress(queue.Full):
            self._queue.put_nowait(None)

    def __enter__(self) -> Subscription[T]:
        return self

    def __exit__(
        self,
        type_name: type[BaseException] | None,
        value: BaseException | None,
        slad: TracebackType | None,
    ) -> None:
        self.close()

    def __iter__(self) -> Iterator[T]:
        """Events until the subscription closes. A `None` in the queue ends the loop."""
        while True:
            event = self.get(timeout=0.5)
            if event is None:
                if self.closed:
                    return
                continue
            yield event


class Bus[T]:
    """One-to-many broadcast. CORE writes, adapters read.

    One mechanism for the two things CORE lets out: **events** (`Event` — what
    happened) and **traffic** (`Traffic` — what travelled the wire). Both need exactly
    the same backpressure, so they share one piece of code.
    """

    __slots__ = ("_lock", "_subscribers", "published")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: tuple[Subscription[T], ...] = ()
        self.published = 0

    @property
    def listeners(self) -> tuple[Subscription[T], ...]:
        """Who is listening. Empty means **it is not even worth building the message** —
        at five hundred frames per second, expanding names costs."""
        return self._subscribers

    def subscribe(self, depth: int = DEPTH) -> Subscription[T]:
        sub: Subscription[T] = Subscription(self, depth)
        with self._lock:
            self._subscribers = (*self._subscribers, sub)
        return sub

    def unsubscribe(self, sub: Subscription[T]) -> None:
        with self._lock:
            self._subscribers = tuple(s for s in self._subscribers if s is not sub)

    def publish(self, event: T) -> None:
        """Called **from the CORE thread**.

        The listener list is immutable, so it is read without a lock — the same trick
        as with swapping the snapshot ([`ARCHITEKTURA.md`](../ARCHITEKTURA.md) §5.3).
        """
        subscribers = self._subscribers
        if not subscribers:
            return
        self.published += 1
        for sub in subscribers:
            sub.offer(event)


EventBus = Bus[Event]
"""The semantic event bus. The name stayed, because that is how the rest of the hub
sees it."""

__all__ = ["DEPTH", "Bus", "Event", "EventBus", "EventKind", "Subscription"]
