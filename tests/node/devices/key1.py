"""A single-key flush-mounted switch."""

from __future__ import annotations

import time
from collections.abc import Callable

from lagunhub.protocol.endpoints.key_input import HOLD_THRESHOLD, PRESS_COUNT, Feature
from lagunhub.protocol.registry import NodeClass
from tests.node.endpoints.key_input import KeyInput
from tests.node.node import NodeCore

DEVICE_TYPE = 0x01F0


def key1(
    *,
    uid: bytes,
    address: int,
    clock: Callable[[], float] = time.monotonic,
    name: str = "key1",
) -> NodeCore:
    """ep4 KeyInput on channel 1. Class CONTROL — traffic somebody has to react to."""
    return NodeCore(
        uid=uid,
        device_type=DEVICE_TYPE,
        node_class=NodeClass.CONTROL,
        functional={
            # Hold and multi-click, but only the hold threshold is tunable from the
            # hub: the rest the device sets itself, so the hub has to cope with an
            # optional attribute that is not there (types/0x0010-key-input.md §2.4).
            4: KeyInput(
                channel=1,
                features=Feature.HOLD | Feature.MULTICLICK,
                optional=(PRESS_COUNT, HOLD_THRESHOLD),
            )
        },
        address=address,
        clock=clock,
        name=name,
    )
