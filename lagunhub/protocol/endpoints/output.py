"""Type 0x0020 Output — a driven output.

It has state and takes commands, so it **emits no events at all**. One type covers a
relay, a dimmer and an RGBW strip — the features settle which.
"""

from __future__ import annotations

from typing import Final

from lagunhub.protocol.endpoints.definition import AttributeDef as A
from lagunhub.protocol.endpoints.definition import CommandDef as C
from lagunhub.protocol.endpoints.definition import Field as F
from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.registry import EndpointType


class Feature:
    LEVEL = 1 << 0
    COLOR_RGB = 1 << 1
    COLOR_CCT = 1 << 2
    PULSE = 1 << 3


class PowerOnBehaviour:
    OFF = 0
    ON = 1
    RESTORE = 2


class Purpose:
    GENERIC = 0
    LIGHT = 1
    VALVE = 2
    HEATER = 3
    FAN = 4
    BUZZER = 5
    GATE = 6
    PUMP = 7
    INDICATOR = 8


ON_OFF: Final = 0x00
LEVEL: Final = 0x01
RED: Final = 0x02
GREEN: Final = 0x03
BLUE: Final = 0x04
WHITE: Final = 0x05
COLOR_TEMPERATURE: Final = 0x06
TRANSITIONING: Final = 0x07
LAST_LEVEL: Final = 0x08
MIN_LEVEL: Final = 0x10
MAX_LEVEL: Final = 0x11
DEFAULT_FADE_TIME: Final = 0x12
POWER_ON_BEHAVIOUR: Final = 0x13
INVERT: Final = 0x14
PULSE_DURATION: Final = 0x15
AUTO_OFF_TIME: Final = 0x16
PURPOSE: Final = 0x17

OFF: Final = 0x00
ON: Final = 0x01
TOGGLE: Final = 0x02
SET_LEVEL: Final = 0x03
STEP: Final = 0x04
STOP: Final = 0x05
SET_COLOR: Final = 0x06
SET_COLOR_TEMPERATURE: Final = 0x07
PULSE: Final = 0x08

_RGB = Feature.COLOR_RGB
_LVL = Feature.LEVEL

OUTPUT: Final = TypeDef(
    type_id=EndpointType.Output,
    revision=1,
    attributes={
        ON_OFF: A(ON_OFF, "OnOff", "bool", "RO", reported=True),
        LEVEL: A(LEVEL, "Level", "u16", "RO", reported=True, hi=1000, feature=_LVL),
        RED: A(RED, "Red", "u8", "RO", reported=True, feature=_RGB),
        GREEN: A(GREEN, "Green", "u8", "RO", reported=True, feature=_RGB),
        BLUE: A(BLUE, "Blue", "u8", "RO", reported=True, feature=_RGB),
        WHITE: A(WHITE, "White", "u8", "RO", reported=True, feature=_RGB),
        COLOR_TEMPERATURE: A(
            COLOR_TEMPERATURE,
            "ColorTemperature",
            "u16",
            "RO",
            reported=True,
            default=370,
            lo=153,
            hi=500,
            feature=Feature.COLOR_CCT,
        ),
        TRANSITIONING: A(TRANSITIONING, "Transitioning", "bool", "RO", reported=True, feature=_LVL),
        LAST_LEVEL: A(
            LAST_LEVEL,
            "LastLevel",
            "u16",
            "RO",
            persist=True,
            default=1000,
            lo=1,
            hi=1000,
            feature=_LVL,
        ),
        MIN_LEVEL: A(
            MIN_LEVEL,
            "MinLevel",
            "u16",
            "RW",
            persist=True,
            default=10,
            lo=1,
            hi=1000,
            feature=_LVL,
        ),
        MAX_LEVEL: A(
            MAX_LEVEL,
            "MaxLevel",
            "u16",
            "RW",
            persist=True,
            default=1000,
            lo=1,
            hi=1000,
            feature=_LVL,
        ),
        DEFAULT_FADE_TIME: A(
            DEFAULT_FADE_TIME,
            "DefaultFadeTime",
            "u16",
            "RW",
            persist=True,
            default=400,
            hi=60000,
            feature=_LVL,
        ),
        POWER_ON_BEHAVIOUR: A(
            POWER_ON_BEHAVIOUR,
            "PowerOnBehaviour",
            "enum8",
            "RW",
            persist=True,
            default=PowerOnBehaviour.OFF,
        ),
        INVERT: A(INVERT, "Invert", "bool", "RW", persist=True, default=False, optional=True),
        PULSE_DURATION: A(
            PULSE_DURATION,
            "PulseDuration",
            "u16",
            "RW",
            persist=True,
            default=500,
            lo=10,
            hi=60000,
            feature=Feature.PULSE,
        ),
        AUTO_OFF_TIME: A(
            AUTO_OFF_TIME, "AutoOffTime", "u16", "RW", persist=True, hi=65534, optional=True
        ),
        PURPOSE: A(PURPOSE, "Purpose", "enum8", "RW", persist=True, default=Purpose.GENERIC),
    },
    commands={
        OFF: C(OFF, "Off"),
        ON: C(ON, "On"),
        TOGGLE: C(TOGGLE, "Toggle"),
        SET_LEVEL: C(
            SET_LEVEL, "SetLevel", (F("level", "u16"), F("transition", "u16")), feature=_LVL
        ),
        STEP: C(STEP, "Step", (F("delta", "i16"), F("transition", "u16")), feature=_LVL),
        STOP: C(STOP, "Stop", feature=_LVL),
        SET_COLOR: C(
            SET_COLOR,
            "SetColor",
            (F("r", "u8"), F("g", "u8"), F("b", "u8"), F("w", "u8"), F("transition", "u16")),
            feature=_RGB,
        ),
        SET_COLOR_TEMPERATURE: C(
            SET_COLOR_TEMPERATURE,
            "SetColorTemperature",
            (F("mired", "u16"), F("transition", "u16")),
            feature=Feature.COLOR_CCT,
        ),
        PULSE: C(PULSE, "Pulse", (F("duration", "u16"),), feature=Feature.PULSE),
    },
)
