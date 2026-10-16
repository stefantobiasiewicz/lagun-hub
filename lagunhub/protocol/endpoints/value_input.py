"""Type 0x0011 ValueInput — an input that holds a number of its own: an encoder, a key
pair, a slider (types/0x0011-value-input.md).

The type describes the **value**, not the finger (zadania/zrobione/CR-04.md): no features,
no hold, no debounce. `Value` is in ‰ like `Output.Level`, so a binding is a copy, not a
conversion (§5.1) — `Changed.value` goes into `SetLevel.level` as it is
(07-hub.md §3.2, zadania/zrobione/DEVICE-02.md N1).

Two rules between attributes the table cannot say: `MinValue ≤ MaxValue`, and `Value`
within them. The node enforces both (`INVALID_VALUE`, and a narrowed range clips `Value`,
DEVICE-02 S1); the hub only describes the ranges.
"""

from __future__ import annotations

from typing import Final

from lagunhub.protocol.endpoints.definition import AttributeDef as A
from lagunhub.protocol.endpoints.definition import EventDef as E
from lagunhub.protocol.endpoints.definition import Field as F
from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.registry import EndpointType

VALUE: Final = 0x00
CHANGE_COUNT: Final = 0x01
MIN_VALUE: Final = 0x10
MAX_VALUE: Final = 0x11
STEP_SIZE: Final = 0x12
WRAP: Final = 0x18

CHANGED: Final = 0x01

VALUE_INPUT: Final = TypeDef(
    type_id=EndpointType.ValueInput,
    revision=1,
    attributes={
        VALUE: A(VALUE, "Value", "u16", "RW", persist=True, reported=True, hi=1000),
        CHANGE_COUNT: A(CHANGE_COUNT, "ChangeCount", "u32", "RO", persist=True, optional=True),
        MIN_VALUE: A(MIN_VALUE, "MinValue", "u16", "RW", persist=True, default=0, hi=1000),
        MAX_VALUE: A(MAX_VALUE, "MaxValue", "u16", "RW", persist=True, default=1000, hi=1000),
        STEP_SIZE: A(
            STEP_SIZE,
            "StepSize",
            "u16",
            "RW",
            persist=True,
            default=50,
            lo=1,
            hi=1000,
            optional=True,
        ),
        WRAP: A(WRAP, "Wrap", "bool", "RW", persist=True, optional=True),
    },
    events={
        # `delta` is arithmetic, a wrap included — 1000 → 0 is −1000 (§4, DEVICE-02 S2).
        CHANGED: E(CHANGED, "Changed", (F("value", "u16"), F("delta", "i16"))),
    },
)
