"""A single-channel DIN-rail relay."""

from __future__ import annotations

import time
from collections.abc import Callable

from lagunhub.protocol.endpoints.output import AUTO_OFF_TIME, PURPOSE, Purpose
from lagunhub.protocol.registry import NodeClass
from tests.node.endpoints.output import Output
from tests.node.node import NodeCore

DEVICE_TYPE = 0x02F0


def relay1(
    *,
    uid: bytes,
    address: int,
    purpose: int = Purpose.LIGHT,
    clock: Callable[[], float] = time.monotonic,
    name: str = "relay1",
) -> NodeCore:
    """ep4 Output on channel 1, no features — the bare core of the type.

    The node has an output, so ``NodeCore`` will add ep3 Scenes to it. There is no need
    to remember that here, and that is exactly the point.
    """
    return NodeCore(
        uid=uid,
        device_type=DEVICE_TYPE,
        node_class=NodeClass.CONTROL,
        functional={4: Output(channel=1, overrides={PURPOSE: purpose}, optional=(AUTO_OFF_TIME,))},
        address=address,
        clock=clock,
        name=name,
    )
