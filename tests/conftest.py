"""The shared cast of the hub's tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass
class Clock:
    """A clock driven from the test. Nothing in the tests asks for real time."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()
