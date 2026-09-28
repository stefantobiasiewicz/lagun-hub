"""The behaviour of the Groups type. Definitions: lagunhub.protocol.endpoints.groups.

The node declares ``GroupCapacity = 0``: the endpoint's presence is mandatory,
supporting groups is not. The table comes together with group commands on the hub's side.
"""

from __future__ import annotations

from typing import ClassVar

from lagunhub.protocol.endpoints.definition import CommandDef, TypeDef
from lagunhub.protocol.endpoints.groups import GROUP_CAPACITY, GROUPS
from lagunhub.protocol.registry import ResultCode
from lagunhub.protocol.values import Value
from tests.node.endpoints.base import SystemEndpoint


class Groups(SystemEndpoint):
    TYPE: ClassVar[TypeDef] = GROUPS

    def on_command(self, command: CommandDef, args: dict[str, Value]) -> tuple[ResultCode, bytes]:
        del command, args
        if self.num(GROUP_CAPACITY) == 0:
            # The node works correctly and says it has nowhere to put this
            # (04-interakcje.md §9).
            return ResultCode.INSUFFICIENT_SPACE, b""
        return ResultCode.UNSUPPORTED_ELEMENT, b""
