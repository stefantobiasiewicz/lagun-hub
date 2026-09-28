"""A single-key flush-mounted switch."""

from __future__ import annotations

import time
from collections.abc import Callable

from lagunhub.protocol.endpoints.key_input import PRESS_COUNT
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
        functional={4: KeyInput(channel=1, optional=(PRESS_COUNT,))},
        address=address,
        clock=clock,
        name=name,
    )
