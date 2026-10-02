"""Traffic per bus — frames counted where they pass, and the rate read from the counts.

The operator is to see a line filling up — a node with too short a `ReportInterval`, a
broken key spraying events — **before** commands start arriving late
([`zadania/zrobione/FEAT-05.md`](../../../zadania/zrobione/FEAT-05.md)).

**The counters are the thing, the rate is only one reading of them.** They grow from the
hub's start and are never reset, so anybody reading `GET /health` twice can work out any
window they like — a minute, an hour, a day's histogram — without the hub keeping a
history (that is [`zadania/FEAT-08.md`](../../../zadania/FEAT-08.md)). A rate computed
inside the hub and nothing else would have fixed one window for everybody.

**Every `BusCounts` has exactly one writer thread.** Received frames are counted by the
RX thread, sent ones twice over: the fast path's sends by the RX thread (in the binding
engine), CORE's own by CORE. Summing happens on reading. That is what makes the counting
free of locks, the same way as `Latency.record` — one writer per integer, readers on
another thread see a value at most a frame old. One shared counter for both senders
would have needed a lock on the fast path.

Counting happens **where the frame passes**, not on the far side of the slow queue: the
queue is filled with `put_nowait`, and when it overflows what it loses is exactly the
traffic of the busiest moment.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from lagunhub.protocol.frame import MAX_PAYLOAD, Frame

SAMPLE: Final = 1.0
"""How often CORE takes a sample of the counters, in seconds."""

WINDOW: Final = 3.0
"""The rate is the difference across this many seconds. Three seconds, not one: a single
second jumps between the view's refreshes (every 2 s), while a minute lags behind a
burst the operator is watching happen. Longer windows belong to whoever collects the
counters (zadania/FEAT-08.md)."""


def _frame_bits(payload: int) -> int:
    """The bits one CAN 2.0B frame with an extended identifier takes on the wire, **at
    worst**.

    Unstuffed: SOF 1, identifier 11 + SRR 1 + IDE 1 + 18, RTR 1, r1 r0 2, DLC 4, data,
    CRC 15 — 54 + 8·n bits, all of them subject to stuffing — then CRC delimiter 1,
    ACK 2, EOF 7 and the interframe space 3: 67 + 8·n. Stuffing adds one bit after
    every five equal ones; the worst case is one in four after the first, so
    (54 + 8·n − 1) // 4 more.

    The worst case rather than the real one, and on purpose. The real number of stuff
    bits depends on the bit pattern, CRC included — working it out would cost a bit-level
    pass over every frame on the fast path. The bare frame without stuffing understates
    the load, and the question being asked is "is this line filling up", where erring
    high is the safe side. Hence the `≈` wherever the load is shown.
    """
    unstuffed = 54 + 8 * payload
    return unstuffed + 13 + (unstuffed - 1) // 4


FRAME_BITS: Final = tuple(_frame_bits(n) for n in range(MAX_PAYLOAD + 1))
"""`FRAME_BITS[len(data)]` — a lookup, because it is added up on the fast path."""


class BusCounts:
    """Counters per bus, written by **one** thread. Grow only."""

    __slots__ = ("bits", "dropped", "frames")

    def __init__(self, buses: int) -> None:
        self.frames = [0] * buses
        self.bits = [0] * buses
        """The wire time the frames took, at worst — the load is computed from it."""

        self.dropped = [0] * buses
        """Sends refused by a full transmit queue. Only senders have these."""

    def count(self, bus: int, frame: Frame) -> None:
        """Called on the fast path. Two additions and a lookup, nothing more."""
        self.frames[bus] += 1
        self.bits[bus] += FRAME_BITS[len(frame.data)]

    def drop(self, bus: int) -> None:
        self.dropped[bus] += 1


@dataclass(frozen=True, slots=True)
class BusReading:
    """One bus at one moment: counters since the start, and the rate across the window."""

    rx: int
    tx: int
    dropped: int
    rx_rate: float
    tx_rate: float
    bits_rate: float
    """Wire bits per second, both directions — they share one wire."""


@dataclass(frozen=True, slots=True)
class _Sample:
    at: float
    rx: int
    tx: int
    bits: int


class BusMeter:
    """Gathers the counters of every thread and turns them into a rate.

    Owned by CORE, which samples it on its own deadline and reads it for `health()` — both
    on the CORE thread, so the samples need no lock either.
    """

    def __init__(self, buses: int) -> None:
        self.received: list[BusCounts] = []
        self.sent: list[BusCounts] = []
        depth = round(WINDOW / SAMPLE) + 1  # three intervals need four samples
        self._samples: list[deque[_Sample]] = [deque(maxlen=depth) for _ in range(buses)]

    def watch(self, *, received: Iterable[BusCounts] = (), sent: Iterable[BusCounts] = ()) -> None:
        """Add counters to read. CORE adds its own; whoever starts the RX thread adds the RX
        thread's and the binding engine's."""
        self.received.extend(received)
        self.sent.extend(sent)

    def sample(self, now: float) -> None:
        for bus, samples in enumerate(self._samples):
            rx, tx, bits = self._totals(bus)
            samples.append(_Sample(now, rx, tx, bits))

    def reading(self, bus: int) -> BusReading:
        rx, tx, _ = self._totals(bus)
        dropped = sum(counts.dropped[bus] for counts in self.sent)
        samples = self._samples[bus]
        if len(samples) < 2 or samples[-1].at <= samples[0].at:
            return BusReading(rx, tx, dropped, 0.0, 0.0, 0.0)
        first, last = samples[0], samples[-1]
        span = last.at - first.at
        return BusReading(
            rx,
            tx,
            dropped,
            rx_rate=(last.rx - first.rx) / span,
            tx_rate=(last.tx - first.tx) / span,
            bits_rate=(last.bits - first.bits) / span,
        )

    def _totals(self, bus: int) -> tuple[int, int, int]:
        rx = sum(counts.frames[bus] for counts in self.received)
        tx = sum(counts.frames[bus] for counts in self.sent)
        bits = sum(counts.bits[bus] for counts in (*self.received, *self.sent))
        return rx, tx, bits
