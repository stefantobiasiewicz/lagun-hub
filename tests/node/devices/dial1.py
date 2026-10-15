"""A knob: an encoder with a key in its shaft, as `devices/apps/dial`."""

from __future__ import annotations

import time
from collections.abc import Callable

from lagunhub.protocol.endpoints.key_input import Feature
from lagunhub.protocol.endpoints.value_input import CHANGE_COUNT, STEP_SIZE, WRAP
from lagunhub.protocol.registry import NodeClass
from tests.node.endpoints.key_input import KeyInput
from tests.node.endpoints.value_input import ValueInput
from tests.node.node import NodeCore

DEVICE_TYPE = 0x0012


def dial1(
    *,
    uid: bytes,
    address: int,
    clock: Callable[[], float] = time.monotonic,
    name: str = "dial1",
) -> NodeCore:
    """ep4 ValueInput, ep5 KeyInput, both on channel 1 — one thing a person touches
    (01-model-danych.md §5). `StepSize` 20 ‰, the encoder's (§6)."""
    return NodeCore(
        uid=uid,
        device_type=DEVICE_TYPE,
        node_class=NodeClass.CONTROL,
        functional={
            4: ValueInput(
                channel=1,
                optional=(CHANGE_COUNT, STEP_SIZE, WRAP),
                overrides={STEP_SIZE: 20},
            ),
            5: KeyInput(channel=1, features=Feature.HOLD | Feature.MULTICLICK),
        },
        address=address,
        clock=clock,
        name=name,
    )
