"""CORE deadlines — without a clock thread of its own.

CORE waits on a queue **with a timeout derived from the nearest deadline**, so one
thread handles transaction retries, discarding segments, expiring joining mode and
detecting absence. A separate timer thread would be a second clock
beside the first ([`ARCHITEKTURA.md`](../ARCHITEKTURA.md) §7.2).
"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

FOREVER: Final = 3600.0
"""How long to wait when there is no deadline at all. Finite, so the loop can be
stopped."""


@dataclass(order=True)
class _Entry:
    when: float
    seq: int
    action: Callable[[], None] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


class Handle:
    """A handle for cancelling a deadline that has not fired yet."""

    __slots__ = ("_entry",)

    def __init__(self, entry: _Entry) -> None:
        self._entry = entry

    def cancel(self) -> None:
        self._entry.cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._entry.cancelled


class Schedule:
    """A heap of deadlines. Not thread-safe — it belongs to CORE alone."""

    def __init__(self) -> None:
        self._heap: list[_Entry] = []
        self._seq = itertools.count()

    def at(self, when: float, action: Callable[[], None]) -> Handle:
        entry = _Entry(when=when, seq=next(self._seq), action=action)
        heapq.heappush(self._heap, entry)
        return Handle(entry)

    def after(self, now: float, delay: float, action: Callable[[], None]) -> Handle:
        return self.at(now + delay, action)

    def next_deadline(self, now: float) -> float:
        """How many seconds one may wait on the queue. Never negative."""
        self._drop_cancelled()
        if not self._heap:
            return FOREVER
        return max(0.0, self._heap[0].when - now)

    def fire_due(self, now: float) -> int:
        """Run everything whose deadline has passed. Returns how many ran."""
        performed = 0
        while self._heap and self._heap[0].when <= now:
            entry = heapq.heappop(self._heap)
            if entry.cancelled:
                continue
            entry.action()
            performed += 1
        return performed

    def _drop_cancelled(self) -> None:
        while self._heap and self._heap[0].cancelled:
            heapq.heappop(self._heap)

    def __len__(self) -> int:
        return sum(1 for entry in self._heap if not entry.cancelled)
