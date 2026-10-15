"""The behaviour of the ValueInput type. Definitions: lagunhub.protocol.endpoints.value_input.

The double turns like the knob in `lagunbus-node` (`types/value_input.c`) does, minus
the storing: a step past a limit reaches it, at the limit nothing is said, with `Wrap`
only a step FROM a limit wraps, and `delta` is arithmetic
(types/0x0011-value-input.md §5.2; zadania/devices/DEVICE-02.md S2). Several steps at
once are one `Changed` (N11). A narrowed range clips `Value` (S1).
"""

from __future__ import annotations

from typing import ClassVar

from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.endpoints.value_input import (
    CHANGE_COUNT,
    CHANGED,
    MAX_VALUE,
    MIN_VALUE,
    STEP_SIZE,
    VALUE,
    VALUE_INPUT,
    WRAP,
)
from lagunhub.protocol.registry import ResultCode
from lagunhub.protocol.values import Value, decode, encode
from tests.node.endpoints.base import FunctionalEndpoint

OWN_STEP = 20
"""The double's step when it does not expose `StepSize` — an encoder's (§6)."""


class ValueInput(FunctionalEndpoint):
    """A knob. ``turn()`` is a person turning it; a write is the hub."""

    TYPE: ClassVar[TypeDef] = VALUE_INPUT

    def write(self, attribute: int, data: bytes) -> ResultCode:
        # The rules between attributes the catalogue cannot say (§2.2, S1): Min ≤ Max,
        # Value within them. The rest — access, type, the table's range — is the base's.
        if attribute in (VALUE, MIN_VALUE, MAX_VALUE) and len(data) == 2:
            v = int(decode("u16", data))  # type: ignore[arg-type]
            lo, hi = self.num(MIN_VALUE), self.num(MAX_VALUE)
            if (
                (attribute == VALUE and not lo <= v <= hi)
                or (attribute == MIN_VALUE and v > hi)
                or (attribute == MAX_VALUE and v < lo)
            ):
                return ResultCode.INVALID_VALUE
        return super().write(attribute, data)

    def on_write(self, attribute: int, value: Value) -> None:
        del value
        if attribute in (MIN_VALUE, MAX_VALUE):
            now = self.num(VALUE)
            clipped = min(max(now, self.num(MIN_VALUE)), self.num(MAX_VALUE))
            self.set(VALUE, clipped)  # reported, no Changed — not a person (§5.5)

    def turn(self, steps: int) -> bool:
        """`steps` detents, negative down, as ONE change. Returns whether `Value` moved."""
        lo, hi = self.num(MIN_VALUE), self.num(MAX_VALUE)
        step = self.opt(STEP_SIZE, OWN_STEP)
        wrap = bool(self.opt(WRAP))
        before = after = self.num(VALUE)
        for _ in range(min(abs(steps), 1000)):
            if steps > 0:
                after = (lo if wrap else hi) if after >= hi else min(after + step, hi)
            else:
                after = (hi if wrap else lo) if after <= lo else max(after - step, lo)
        if after == before:
            return False
        if CHANGE_COUNT in self.spec:
            self.values[CHANGE_COUNT] = self.num(CHANGE_COUNT) + 1
        self.emit(CHANGED, encode("u16", after) + encode("i16", after - before))
        self.set(VALUE, after)
        return True
