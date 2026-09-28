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

CAPACITY: Final = 8
"""As many as there are Tid values. A smaller buffer would throw out a command still
inside the window — and then its repetition would be carried out a second time."""

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
