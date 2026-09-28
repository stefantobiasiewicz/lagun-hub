"""The behaviour of the Output type. Definitions: lagunhub.protocol.endpoints.output.

A command's effect shows in the attribute's report, not in the reply to it — and that is
the distinction the node double has to keep, so as not to teach the hub bad habits.
"""

from __future__ import annotations

from typing import ClassVar

from lagunhub.protocol.endpoints.definition import CommandDef, TypeDef
from lagunhub.protocol.endpoints.output import (
    AUTO_OFF_TIME,
    LEVEL,
    OFF,
    ON,
    ON_OFF,
    OUTPUT,
    SET_LEVEL,
    TOGGLE,
)
from lagunhub.protocol.registry import ResultCode
from lagunhub.protocol.values import Value
from tests.node.endpoints.base import FunctionalEndpoint


class Output(FunctionalEndpoint):
    TYPE: ClassVar[TypeDef] = OUTPUT

    def __post_init__(self) -> None:
        super().__post_init__()
        self._auto_off_at: float | None = None

    def on_command(self, command: CommandDef, args: dict[str, Value]) -> tuple[ResultCode, bytes]:
        if command.id in (OFF, ON, TOGGLE):
            state = {OFF: False, ON: True}.get(command.id, not self.values[ON_OFF])
            self._switch(bool(state))
            return ResultCode.OK, b""
        if command.id == SET_LEVEL:
            level = int(args["level"])  # type: ignore[arg-type]
            if level > 1000:
                return ResultCode.INVALID_VALUE, b""
            self.set(LEVEL, level)
            self._switch(level > 0)
            return ResultCode.OK, b""
        return ResultCode.UNSUPPORTED_ELEMENT, b""

    def _switch(self, state: bool) -> None:
        self.set(ON_OFF, state)
        auto_off = self.opt(AUTO_OFF_TIME)
        if state and auto_off and self.node is not None:
            self._auto_off_at = self.node.clock() + auto_off
        else:
            self._auto_off_at = None

    def tick(self, now: float) -> None:
        if self._auto_off_at is not None and now >= self._auto_off_at:
            self._auto_off_at = None
            self.set(ON_OFF, False)
