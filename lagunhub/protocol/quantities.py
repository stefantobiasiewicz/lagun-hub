"""Physical quantities and their scales — lagunbus-spec/02-typy-danych.md §3.

**The scale is a property of the quantity, not of the attribute.** Every temperature
in the whole specification is an ``i16`` scaled by 0.01 °C — no exceptions and no
negotiation.

A scale once settled is settled forever: changing it is a backwards-incompatible
change and requires a **new type identifier**, because a node in a flush-mounted box
may never be updated (§6).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Final


class Quantity(IntEnum):
    """The enumeration from types/0x0030-sense.md §2.3."""

    TEMPERATURE = 0x00
    HUMIDITY = 0x01
    ILLUMINANCE = 0x02
    CO2 = 0x03
    VOC = 0x04
    PM25 = 0x05
    PRESSURE = 0x06
    FLOW = 0x07
    SOIL_MOISTURE = 0x08
    VOLTAGE = 0x09
    CURRENT = 0x0A
    POWER = 0x0B
    DISTANCE = 0x0C


@dataclass(frozen=True, slots=True)
class Scale:
    type: str
    """The carrying type — the length of the value in a frame follows from it."""

    step: float
    """How much of the physical unit one integer step means."""

    unit: str


SCALES: Final[dict[Quantity, Scale]] = {
    Quantity.TEMPERATURE: Scale("i16", 0.01, "°C"),
    Quantity.HUMIDITY: Scale("u16", 0.01, "%"),
    Quantity.ILLUMINANCE: Scale("u32", 0.01, "lx"),
    Quantity.CO2: Scale("u16", 1.0, "ppm"),
    Quantity.VOC: Scale("u16", 1.0, "ppb"),
    Quantity.PM25: Scale("u16", 0.1, "µg/m³"),
    Quantity.PRESSURE: Scale("u32", 0.01, "hPa"),
    Quantity.FLOW: Scale("u32", 0.001, "L/min"),
    Quantity.SOIL_MOISTURE: Scale("u16", 0.01, "%"),
    Quantity.VOLTAGE: Scale("u32", 0.001, "V"),
    Quantity.CURRENT: Scale("i32", 0.001, "A"),
    Quantity.POWER: Scale("i32", 0.1, "W"),
    Quantity.DISTANCE: Scale("u32", 1.0, "mm"),
}

# Cumulative quantities — the ones that only ever grow — do not belong to Sense.
# The Meter type handles them, and there the direction is an attribute rather than a
# separate endpoint.
CUMULATIVE: Final[dict[str, Scale]] = {
    "ENERGY": Scale("u32", 1.0, "Wh"),
    "VOLUME": Scale("u32", 1.0, "L"),
}


def raw_type(quantity: Quantity) -> str:
    """The carrying type of a quantity — it settles how many bytes it takes in a frame."""
    return SCALES[quantity].type


def to_physical(quantity: Quantity, raw: int) -> float:
    """``2150`` with ``TEMPERATURE`` → ``21.5``. For showing, never for control."""
    return raw * SCALES[quantity].step


def from_physical(quantity: Quantity, value: float) -> int:
    """``21.5`` with ``TEMPERATURE`` → ``2150``.

    It rounds, because there are no floating-point numbers on the bus — and integers
    compare exactly, so hysteresis and thresholds behave predictably.
    """
    return round(value / SCALES[quantity].step)


def format_value(quantity: Quantity, raw: int) -> str:
    scale = SCALES[quantity]
    places = max(0, len(str(scale.step).split(".")[-1])) if scale.step < 1 else 0
    return f"{to_physical(quantity, raw):.{places}f} {scale.unit}"
