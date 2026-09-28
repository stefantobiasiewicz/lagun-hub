"""The registry is generated from the specification, so a test keeps it from drifting."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lagunhub.protocol.registry import (
    FIRST_FUNCTIONAL_ENDPOINT,
    NODE_CLASS_BLOCKS,
    RETRYABLE,
    SYSTEM_ENDPOINTS,
    TRANSACTIONAL,
    CommonAttribute,
    EndpointType,
    MessageKind,
    NodeClass,
    ResultCode,
)

HUB = Path(__file__).resolve().parent.parent


def test_the_file_agrees_with_the_spec_registry() -> None:
    """A change in registry/*.yaml without regenerating is to break the build."""
    result = subprocess.run(
        [sys.executable, "tools/generate_registry.py", "--check"],
        cwd=HUB,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_the_priority_ladder_after_renumbering() -> None:
    """Values from before decision W15 went round the specification — we watch them."""
    assert MessageKind.ALARM.value == 0x0
    assert MessageKind.EVENT.value == 0x1
    assert MessageKind.COMMAND.value == 0x3
    assert MessageKind.RESPONSE.value == 0x7
    assert MessageKind.REPORT.value == 0x8
    assert MessageKind.LONG_RESPONSE.value == 0x9
    assert MessageKind.LONG_WRITE.value == 0xA
    assert MessageKind.JOIN.value == 0xF


def test_the_high_reserve_stays_empty() -> None:
    """0x2 is deliberately free — reserve for control from a node
    (05-mapowanie-can.md §2.3)."""
    assert 0x2 not in {k.value for k in MessageKind}
    assert 0xD not in {k.value for k in MessageKind}
    assert 0xE not in {k.value for k in MessageKind}


def test_the_kind_number_is_the_priority() -> None:
    assert MessageKind.ALARM < MessageKind.EVENT < MessageKind.COMMAND
    assert MessageKind.RESPONSE < MessageKind.REPORT < MessageKind.LONG_RESPONSE


def test_transactional_kinds() -> None:
    """They alone carry a Tid in byte [0] (04-interakcje.md §11)."""
    assert {
        MessageKind.COMMAND,
        MessageKind.WRITE,
        MessageKind.READ,
        MessageKind.RESPONSE,
        MessageKind.QUERY,
        MessageKind.HUB_RESPONSE,
    } == TRANSACTIONAL
    assert MessageKind.GROUP_COMMAND not in TRANSACTIONAL
    assert MessageKind.REPORT not in TRANSACTIONAL


def test_busy_is_the_only_retryable_one() -> None:
    """The remaining errors repeated will give the same result (04-interakcje.md §9)."""
    assert {ResultCode.BUSY} == RETRYABLE
    assert ResultCode.OK.value == 0x00
    assert ResultCode.SEGMENTED.value == 0x0B
    assert ResultCode.UNAVAILABLE.value == 0x0C


def test_address_blocks() -> None:
    assert NODE_CLASS_BLOCKS[NodeClass.CONTROL] == (0x001, 0x0FF)
    assert NODE_CLASS_BLOCKS[NodeClass.SENSOR] == (0x100, 0x3FF)
    assert NODE_CLASS_BLOCKS[NodeClass.UNSPECIFIED] == (0x400, 0x7FD)


def test_address_blocks_do_not_overlap() -> None:
    taken: set[int] = set()
    for lo, hi in NODE_CLASS_BLOCKS.values():
        block = set(range(lo, hi + 1))
        assert not (block & taken)
        taken |= block


def test_system_endpoints() -> None:
    """The specification fixes the number and the type, not the device
    (01-model-danych.md §1.2)."""
    assert {
        0: EndpointType.Device,
        1: EndpointType.Diagnostics,
        2: EndpointType.Groups,
        3: EndpointType.Scenes,
    } == SYSTEM_ENDPOINTS
    assert FIRST_FUNCTIONAL_ENDPOINT == 4
    assert max(SYSTEM_ENDPOINTS) == FIRST_FUNCTIONAL_ENDPOINT - 1


def test_types_needed_for_the_poc() -> None:
    assert EndpointType.KeyInput.value == 0x0010
    assert EndpointType.Output.value == 0x0020


def test_common_attributes_sit_in_the_upper_range() -> None:
    """0xF0–0xFF, so that they never clash with a type's attributes
    (01-model-danych.md §4)."""
    assert all(0xF0 <= a.value <= 0xFF for a in CommonAttribute)
    assert CommonAttribute.TypeId.value == 0xF0
    assert CommonAttribute.Channel.value == 0xF5
    assert CommonAttribute.QueryEnabled.value == 0xF6
    assert CommonAttribute.ReportInterval.value == 0xF8
