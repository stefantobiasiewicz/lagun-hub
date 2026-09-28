"""Type 0x0010 KeyInput — a momentary key.

It has no state and takes no commands: it reports facts and nothing else. `Click`
happens **on release**, because one cannot call a click something that is still
going on.
"""

from __future__ import annotations

from typing import Final

from lagunhub.protocol.endpoints.definition import AttributeDef as A
from lagunhub.protocol.endpoints.definition import EventDef as E
from lagunhub.protocol.endpoints.definition import Field as F
from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.registry import EndpointType

PRESS_COUNT: Final = 0x00
DEBOUNCE_TIME: Final = 0x10
EVENT_MODE: Final = 0x11
MULTI_CLICK_WINDOW: Final = 0x12
MAX_CLICKS: Final = 0x13
HOLD_ENABLED: Final = 0x14
HOLD_THRESHOLD: Final = 0x15
HOLD_TICK_INTERVAL: Final = 0x16
INVERT: Final = 0x17

DOWN: Final = 0x01
UP: Final = 0x02
CLICK: Final = 0x03
HOLD_START: Final = 0x05
HOLD_TICK: Final = 0x06
HOLD_END: Final = 0x07


class EventMode:
    CLICK = 0
    MULTICLICK = 1
    RAW = 2


KEY_INPUT: Final = TypeDef(
    type_id=EndpointType.KeyInput,
    revision=1,
    attributes={
        PRESS_COUNT: A(PRESS_COUNT, "PressCount", "u32", "RO", persist=True, optional=True),
        DEBOUNCE_TIME: A(
            DEBOUNCE_TIME, "DebounceTime", "u16", "RW", persist=True, default=15, lo=5, hi=100
        ),
        EVENT_MODE: A(
            EVENT_MODE, "EventMode", "enum8", "RW", persist=True, default=EventMode.CLICK
        ),
        MULTI_CLICK_WINDOW: A(
            MULTI_CLICK_WINDOW,
            "MultiClickWindow",
            "u16",
            "RW",
            persist=True,
            default=350,
            lo=150,
            hi=800,
        ),
        MAX_CLICKS: A(MAX_CLICKS, "MaxClicks", "u8", "RW", persist=True, default=3, lo=2, hi=5),
        HOLD_ENABLED: A(HOLD_ENABLED, "HoldEnabled", "bool", "RW", persist=True, default=True),
        HOLD_THRESHOLD: A(
            HOLD_THRESHOLD, "HoldThreshold", "u16", "RW", persist=True, default=500, lo=200, hi=3000
        ),
        HOLD_TICK_INTERVAL: A(
            HOLD_TICK_INTERVAL,
            "HoldTickInterval",
            "u16",
            "RW",
            persist=True,
            default=200,
            lo=50,
            hi=1000,
        ),
        INVERT: A(INVERT, "Invert", "bool", "RW", persist=True, default=False, optional=True),
    },
    events={
        DOWN: E(DOWN, "Down"),
        UP: E(UP, "Up", (F("duration", "u16"),)),
        CLICK: E(CLICK, "Click", (F("count", "u8"),)),
        HOLD_START: E(HOLD_START, "HoldStart"),
        HOLD_TICK: E(HOLD_TICK, "HoldTick", (F("elapsed", "u16"),)),
        HOLD_END: E(HOLD_END, "HoldEnd", (F("elapsed", "u16"),)),
    },
)
