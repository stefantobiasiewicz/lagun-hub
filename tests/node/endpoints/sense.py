"""The behaviour of the Sense type. Definitions: lagunhub.protocol.endpoints.sense.

The `Value` attribute's type and scale **follow from `Quantity`**, not from the
attribute's definition — temperature is an `i16` scaled by 0.01 °C, current an `i32`
scaled by 0.001 A. That is settled by the core, in `kind_of()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from lagunhub.protocol.endpoints.definition import QUANTITY_TYPE, CommandDef, TypeDef
from lagunhub.protocol.endpoints.sense import QUANTITY, SENSE, VALUE
from lagunhub.protocol.quantities import Quantity
from lagunhub.protocol.registry import ResultCode
from lagunhub.protocol.values import ABSENT, Value
from tests.node.endpoints.base import FunctionalEndpoint


@dataclass
class Sense(FunctionalEndpoint):
    """The quantity is a **parameter of the device**, not a type of its own.

    That way one `Sense` serves a thermometer, a current sensor and a light meter — and
    a hub that knows the type knows them all (01-model-danych.md §2.1).
    """

    TYPE: ClassVar[TypeDef] = SENSE

    quantity: Quantity = Quantity.TEMPERATURE

    def __post_init__(self) -> None:
        super().__post_init__()
        # "Absent" until the first measurement, and a threshold without a value is
        # inactive (types/0x0030-sense.md §5.4, §5.5) — not the zero a plain `<Q>` starts
        # with in the base.
        for attribute, meta in self.spec.items():
            if meta.type == QUANTITY_TYPE:
                self.values[attribute] = ABSENT
        self.values[QUANTITY] = int(self.quantity)

    def measure(self, raw: int) -> None:
        """A fresh reading from the hardware. A report goes out if the value changed."""
        self.set(VALUE, raw)

    def on_command(self, command: CommandDef, args: dict[str, Value]) -> tuple[ResultCode, bytes]:
        del command, args
        return ResultCode.UNSUPPORTED_ELEMENT, b""
