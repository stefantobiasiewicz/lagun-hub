"""The command deduplication buffer — 04-interakcje.md §2.2.

The only place in the whole specification where a node is obliged to remember anything
about the past. A repeated command **is not carried out a second time** — the node sends
the remembered reply back.

Without this a ``Toggle`` repeated after a lost reply returns to where it started, and
the hub gets a picture it will never see on real hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from lagunhub.protocol.registry import ResultCode

WINDOW: Final = 0.300
"""Exactly the budget of three attempts at 100 ms. Every millisecond beyond that eats
into the Tid reserve and protects nothing."""

CAPACITY: Final = 16
"""Every command of the window must fit — a buffer that throws out a command still inside
it carries its repetition out a second time. The specification asks for at least eight
and "two per endpoint that takes commands". Eight is not a ceiling: the hub counts Tid
per endpoint and command (``TidAllocator``), so eight outputs switched at once and a
ninth command right after are all in one window. Sixteen, as in the node library's
``LB_DEDUP_ENTRIES`` (zadania/zrobione/BUG-02.md)."""

Key = tuple[int, int, int]
"""``(Tid, endpoint, command)`` — a Tid alone is not enough, it has only eight values."""


@dataclass
class DedupBuffer:
    window: float = WINDOW
    capacity: int = CAPACITY
    _entries: dict[Key, tuple[float, ResultCode, bytes]] = field(default_factory=dict)

    def lookup(self, key: Key, now: float) -> tuple[ResultCode, bytes] | None:
        self._expire(now)
        entry = self._entries.get(key)
        if entry is None:
            return None
        _, result, data = entry
        return result, data

    def remember(self, key: Key, result: ResultCode, data: bytes, now: float) -> None:
        self._expire(now)
        if key not in self._entries and len(self._entries) >= self.capacity:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            del self._entries[oldest]
        self._entries[key] = (now, result, data)

    def _expire(self, now: float) -> None:
        for key in [k for k, (at, _, _) in self._entries.items() if now - at >= self.window]:
            del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)
