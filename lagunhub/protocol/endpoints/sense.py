"""Type 0x0030 Sense — an analogue measurement of a continuous quantity.

The type and scale of attributes marked ``<Q>`` **follow from ``Quantity``** rather
than from the attribute definition: every temperature in the whole specification is
an ``i16`` scaled by
0,01 °C ([`quantities.py`](../quantities.py)).
"""

from __future__ import annotations

from typing import Final

from lagunhub.protocol.endpoints.definition import AttributeDef as A
from lagunhub.protocol.endpoints.definition import CommandDef as C
from lagunhub.protocol.endpoints.definition import EventDef as E
from lagunhub.protocol.endpoints.definition import Field as F
from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.registry import EndpointType

VALUE: Final = 0x00
QUANTITY: Final = 0x01
MIN_MEASURED: Final = 0x02
MAX_MEASURED: Final = 0x03
OFFSET: Final = 0x10
THRESHOLD_LOW: Final = 0x11
THRESHOLD_HIGH: Final = 0x12
THRESHOLD_HYSTERESIS: Final = 0x13

RESET_MIN_MAX: Final = 0x00
THRESHOLD: Final = 0x01


class Feature:
    MIN_MAX = 1 << 0
    THRESHOLD = 1 << 1


SENSE: Final = TypeDef(
    type_id=EndpointType.Sense,
    revision=1,
    attributes={
        VALUE: A(VALUE, "Value", "<Q>", "RO", reported=True),
        QUANTITY: A(QUANTITY, "Quantity", "enum8", "RO", persist=True),
        MIN_MEASURED: A(
            MIN_MEASURED, "MinMeasured", "<Q>", "RO", persist=True, feature=Feature.MIN_MAX
        ),
        MAX_MEASURED: A(
            MAX_MEASURED, "MaxMeasured", "<Q>", "RO", persist=True, feature=Feature.MIN_MAX
        ),
        OFFSET: A(OFFSET, "Offset", "i16", "RW", persist=True, optional=True),
        THRESHOLD_LOW: A(
            THRESHOLD_LOW, "ThresholdLow", "<Q>", "RW", persist=True, feature=Feature.THRESHOLD
        ),
        THRESHOLD_HIGH: A(
            THRESHOLD_HIGH, "ThresholdHigh", "<Q>", "RW", persist=True, feature=Feature.THRESHOLD
        ),
        THRESHOLD_HYSTERESIS: A(
            THRESHOLD_HYSTERESIS,
            "ThresholdHysteresis",
            "u16",
            "RW",
            persist=True,
            feature=Feature.THRESHOLD,
            # Optional within the feature (CR-04): it guards against the sensor's own
            # noise, which its author knows; the thresholds themselves are use and stay
            # mandatory (types/0x0030-sense.md §5.4).
            optional=True,
        ),
    },
    commands={RESET_MIN_MAX: C(RESET_MIN_MAX, "ResetMinMax", feature=Feature.MIN_MAX)},
    events={THRESHOLD: E(THRESHOLD, "Threshold", (F("zone", "enum8"), F("value", "<Q>")))},
)
