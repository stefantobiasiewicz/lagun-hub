"""A two-field display: a temperature and a 16-byte text line (types/0x0022-display.md §6)."""

from __future__ import annotations

import time
from collections.abc import Callable

from lagunhub.protocol.endpoints.display import CONTENT_TIMEOUT, Feature
from lagunhub.protocol.quantities import Quantity
from lagunhub.protocol.registry import NodeClass
from tests.node.endpoints.display import Display
from tests.node.node import NodeCore

DEVICE_TYPE = 0x02F2


def display2(
    *,
    uid: bytes,
    address: int,
    clock: Callable[[], float] = time.monotonic,
    name: str = "display2",
) -> NodeCore:
    """ep4 a TEMPERATURE field with `ContentTimeout`, ep5 a text line that keeps its own
    120 s. Channel 0: a field belongs to no channel."""
    return NodeCore(
        uid=uid,
        device_type=DEVICE_TYPE,
        node_class=NodeClass.CONTROL,
        functional={
            4: Display(
                features=Feature.VALUE,
                quantity=Quantity.TEMPERATURE,
                optional=(CONTENT_TIMEOUT,),
            ),
            5: Display(features=Feature.TEXT, text_length=16, own_timeout=120.0),
        },
        address=address,
        clock=clock,
        name=name,
    )
