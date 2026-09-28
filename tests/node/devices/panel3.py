"""A three-key panel with backlighting and a thermometer.

The descriptor is taken from
[`01-model-danych.md`](../../../lagunbus-spec/01-model-danych.md) §7 — the same example the
specification uses to explain what `Channel` and `Purpose` are for.

    ep4   KeyInput                             ch1   top key
    ep5   KeyInput                             ch2   middle key
    ep6   KeyInput                             ch3   bottom key
    ep7   Output [LEVEL|COLOR_RGB] INDICATOR   ch0   the whole panel's backlighting
    ep8   Sense  quantity = TEMPERATURE        ch0   thermometer

The backlighting has `Purpose = INDICATOR`, not `LIGHT` — otherwise "turn off all the
lights" would put out the keys in the whole house.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from lagunhub.protocol.endpoints.key_input import PRESS_COUNT
from lagunhub.protocol.endpoints.output import PURPOSE, Feature, Purpose
from lagunhub.protocol.quantities import Quantity
from lagunhub.protocol.registry import NodeClass
from tests.node.endpoints.key_input import KeyInput
from tests.node.endpoints.output import Output
from tests.node.endpoints.sense import Sense
from tests.node.node import NodeCore

DEVICE_TYPE = 0x01F1


def panel3(
    *,
    uid: bytes,
    address: int,
    clock: Callable[[], float] = time.monotonic,
    name: str = "panel3",
) -> NodeCore:
    """Class CONTROL — a key's event must not queue behind a temperature report."""
    return NodeCore(
        uid=uid,
        device_type=DEVICE_TYPE,
        node_class=NodeClass.CONTROL,
        functional={
            4: KeyInput(channel=1, optional=(PRESS_COUNT,)),
            5: KeyInput(channel=2, optional=(PRESS_COUNT,)),
            6: KeyInput(channel=3, optional=(PRESS_COUNT,)),
            7: Output(
                channel=0,
                features=Feature.LEVEL | Feature.COLOR_RGB,
                overrides={PURPOSE: Purpose.INDICATOR},
            ),
            8: Sense(channel=0, quantity=Quantity.TEMPERATURE),
        },
        address=address,
        clock=clock,
        name=name,
    )
