"""The behaviour of the Diagnostics type.
Definitions: lagunhub.protocol.endpoints.diagnostics.

The first three attributes are reported **by obligation**, with a default
``ReportInterval`` of 300 s. Thanks to them there are no silent nodes: a panel nobody
has touched for a week speaks up every five minutes (06-cykl-zycia-urzadzenia.md §5).
"""

from __future__ import annotations

from typing import ClassVar

from lagunhub.protocol.endpoints.definition import CommandDef, TypeDef
from lagunhub.protocol.endpoints.diagnostics import (
    BUS_OFF_COUNT,
    DIAGNOSTICS,
    RESET_COUNTERS,
    RX_ERROR_COUNT,
    TX_DROPPED,
    TX_ERROR_COUNT,
)
from lagunhub.protocol.registry import CommonAttribute, ResultCode
from lagunhub.protocol.values import Value
from tests.node.endpoints.base import SystemEndpoint


class Diagnostics(SystemEndpoint):
    TYPE: ClassVar[TypeDef] = DIAGNOSTICS

    def __post_init__(self) -> None:
        super().__post_init__()
        self.values[CommonAttribute.ReportInterval] = 300
        self.values[CommonAttribute.ReportHysteresis] = 16

    def on_command(self, command: CommandDef, args: dict[str, Value]) -> tuple[ResultCode, bytes]:
        if command.id == RESET_COUNTERS:
            for attribute in (TX_ERROR_COUNT, RX_ERROR_COUNT, BUS_OFF_COUNT, TX_DROPPED):
                self.values[attribute] = 0
            return ResultCode.OK, b""
        return ResultCode.UNSUPPORTED_ELEMENT, b""
