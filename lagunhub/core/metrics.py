"""Measuring fast-path latency.

Built in permanently rather than switched on by a flag: it costs one subtraction per
frame, and degradation is meant to be visible on a chart **before it becomes
noticeable**.

Samples land in a ring buffer and percentiles are computed on demand. That is dearer
than a histogram with fixed buckets, but exact — and computed off the fast path, so it
is in nobody’s way.
"""

from __future__ import annotations

import threading
from typing import Final

CAPACITY: Final = 4096


class Latency:
    """A ring buffer of samples in nanoseconds."""

    __slots__ = ("_buffer", "_index", "_lock", "count", "worst")

    def __init__(self, capacity: int = CAPACITY) -> None:
        self._buffer: list[int] = [0] * capacity
        self._index = 0
        self._lock = threading.Lock()
        self.count = 0
        self.worst = 0

    def record(self, nanoseconds: int) -> None:
        """Called from the RX thread. No lock and no allocation."""
        self._buffer[self._index] = nanoseconds
        self._index = (self._index + 1) % len(self._buffer)
        self.count += 1
        if nanoseconds > self.worst:
            self.worst = nanoseconds

    def percentiles(self) -> dict[str, float]:
        """Microseconds. Computed off the fast path, so sorting is allowed here."""
        with self._lock:
            filled = min(self.count, len(self._buffer))
            samples = sorted(self._buffer[:filled]) if filled else []
        if not samples:
            return {}

        def at(fraction: float) -> float:
            index = min(len(samples) - 1, int(len(samples) * fraction))
            return samples[index] / 1000.0

        return {
            "n": float(self.count),
            "p50": at(0.50),
            "p99": at(0.99),
            "p999": at(0.999),
            "max": self.worst / 1000.0,
        }

    def summary(self) -> str:
        stats = self.percentiles()
        if not stats:
            return "no samples"
        return (
            f"n={int(stats['n'])}  p50={stats['p50']:.0f}µs  "
            f"p99={stats['p99']:.0f}µs  p99.9={stats['p999']:.0f}µs  "
            f"max={stats['max']:.0f}µs"
        )
