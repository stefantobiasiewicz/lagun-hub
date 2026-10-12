"""Type 0x0002 Diagnostics — the health of the node and of the bus. Always on ep1."""

from __future__ import annotations

from typing import Final

from lagunhub.protocol.endpoints.definition import AttributeDef as A
from lagunhub.protocol.endpoints.definition import CommandDef as C
from lagunhub.protocol.endpoints.definition import EventDef as E
from lagunhub.protocol.endpoints.definition import Field as F
from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.registry import EndpointType

TX_ERROR_COUNT: Final = 0x00
RX_ERROR_COUNT: Final = 0x01
BUS_STATE: Final = 0x02
BUS_OFF_COUNT: Final = 0x03
TX_FRAMES: Final = 0x04
RX_FRAMES: Final = 0x05
TX_DROPPED: Final = 0x06
RX_OVERRUNS: Final = 0x07
SUPPLY_VOLTAGE: Final = 0x10
MCU_TEMPERATURE: Final = 0x11
ERROR_THRESHOLD: Final = 0x20

RESET_COUNTERS: Final = 0x00

BUS_DEGRADED: Final = 0x01
BUS_RECOVERED: Final = 0x02
QUEUE_OVERFLOW: Final = 0x03


class BusState:
    """`BusState` values (types/0x0002-diagnostics.md §2.4). Until 2026-10-03 this had a
    `WARNING` at 1 and everything after it shifted by one, like the node library — both
    agreed with each other and not with the specification (zadania/zrobione/CR-05.md). Warning is
    covered by `ErrorThreshold`, not by a state."""

    ACTIVE = 0
    PASSIVE = 1
    BUS_OFF = 2


DIAGNOSTICS: Final = TypeDef(
    type_id=EndpointType.Diagnostics,
    revision=1,
    attributes={
        TX_ERROR_COUNT: A(TX_ERROR_COUNT, "TxErrorCount", "u16", "RO", reported=True),
        RX_ERROR_COUNT: A(RX_ERROR_COUNT, "RxErrorCount", "u16", "RO", reported=True),
        BUS_STATE: A(BUS_STATE, "BusState", "enum8", "RO", reported=True),
        BUS_OFF_COUNT: A(BUS_OFF_COUNT, "BusOffCount", "u16", "RO", persist=True),
        TX_FRAMES: A(TX_FRAMES, "TxFrames", "u32", "RO", optional=True),
        RX_FRAMES: A(RX_FRAMES, "RxFrames", "u32", "RO", optional=True),
        TX_DROPPED: A(TX_DROPPED, "TxDropped", "u16", "RO", persist=True),
        RX_OVERRUNS: A(RX_OVERRUNS, "RxOverruns", "u16", "RO", persist=True, optional=True),
        SUPPLY_VOLTAGE: A(
            SUPPLY_VOLTAGE, "SupplyVoltage", "u32", "RO", reported=True, optional=True
        ),
        MCU_TEMPERATURE: A(MCU_TEMPERATURE, "McuTemperature", "i16", "RO", optional=True),
        # Optional since CR-04: the threshold follows from the controller, so its author
        # sets it; a node without the attribute keeps a fixed one within 16-255
        # (01-model-danych.md §8.1).
        ERROR_THRESHOLD: A(
            ERROR_THRESHOLD,
            "ErrorThreshold",
            "u16",
            "RW",
            persist=True,
            default=96,
            lo=16,
            hi=255,
            optional=True,
        ),
    },
    commands={RESET_COUNTERS: C(RESET_COUNTERS, "ResetCounters")},
    events={
        BUS_DEGRADED: E(BUS_DEGRADED, "BusDegraded", (F("tec", "u16"), F("rec", "u16"))),
        BUS_RECOVERED: E(
            BUS_RECOVERED, "BusRecovered", (F("busOffCount", "u16"), F("downtime", "u16"))
        ),
        QUEUE_OVERFLOW: E(QUEUE_OVERFLOW, "QueueOverflow", (F("dropped", "u16"),)),
    },
)
