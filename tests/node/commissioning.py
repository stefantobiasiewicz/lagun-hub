"""Joining, from the node's side — 03-adresacja.md §5.

A node that has an address in persistent memory **takes no part in this**: it starts,
emits `Booted` and works. That matters a great deal for the load — a wave of
announcements happens only at an installation's first start or after a mass reset.
An ordinary return of power does not cause one.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Final

DELAYS: Final[tuple[tuple[int, float], ...]] = (
    (5, 2.0),
    (10, 10.0),
)
"""Attempts 1–5 draw from 0…2 s, 6–10 from 0…10 s, later from 0…60 s."""

MAX_DELAY: Final = 60.0
ASSIGN_WINDOW: Final = 0.500
"""How long the node waits for an assignment before it tries again."""


@dataclass
class Joining:
    """The state of a node WITHOUT AN ADDRESS. The delay grows with every failed
    attempt."""

    rng: random.Random = field(default_factory=random.Random)
    attempts: int = 0
    next_attempt: float = 0.0

    def window(self) -> float:
        for limit, window in DELAYS:
            if self.attempts < limit:
                return window
        return MAX_DELAY

    def schedule_next(self, now: float) -> None:
        """A growing interval protects the bus when there is no hub at all.

        At the upper threshold forty nodes make less than one frame a second from the
        whole installation — negligible traffic. The node tries **without end**: the hub
        may be off for a week and the installation is to come up by itself.
        """
        self.attempts += 1
        self.next_attempt = now + self.rng.uniform(0.0, self.window())

    def due(self, now: float) -> bool:
        return now >= self.next_attempt
