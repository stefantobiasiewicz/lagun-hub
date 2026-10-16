"""The behaviour of the Output type. Definitions: lagunhub.protocol.endpoints.output.

A command's effect shows in the attribute's report, not in the reply to it — and that is
the distinction the node double has to keep, so as not to teach the hub bad habits.

With `LEVEL` and `COLOR_RGB` the double keeps the contract of states — `Level` the only
true one, `LastLevel` never zero, `MinLevel`/`MaxLevel`, `Step` never through zero, the
colour independent of the level (types/0x0020-output.md §5.1, §5.2, §5.8) — but every
change is AT ONCE: transitions are the node library's, pinned by its scenarios
(lagunbus-node/vectors/scenarios/output_level.json), and nothing in the hub waits on one.
"""

from __future__ import annotations

from typing import ClassVar

from lagunhub.protocol.endpoints.definition import CommandDef, TypeDef
from lagunhub.protocol.endpoints.output import (
    AUTO_OFF_TIME,
    BLUE,
    GREEN,
    LAST_LEVEL,
    LEVEL,
    MAX_LEVEL,
    MIN_LEVEL,
    OFF,
    ON,
    ON_OFF,
    OUTPUT,
    RED,
    SET_COLOR,
    SET_LEVEL,
    STEP,
    STOP,
    TOGGLE,
    WHITE,
    Feature,
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
            if self.has(Feature.LEVEL):
                self._level(self.num(LAST_LEVEL) if state else 0)
            else:
                self._switch(bool(state))
            return ResultCode.OK, b""
        if command.id == SET_LEVEL:
            level = int(args["level"])  # type: ignore[arg-type]
            if level > 1000:
                return ResultCode.INVALID_VALUE, b""
            self._level(self._bounded(level) if level else 0)
            return ResultCode.OK, b""
        if command.id == STEP:
            now = self.num(LEVEL)
            delta = int(args["delta"])  # type: ignore[arg-type]
            if now or delta > 0:
                self._level(self._bounded(now + delta))
            return ResultCode.OK, b""
        if command.id == STOP:
            return ResultCode.OK, b""  # nothing is ever under way here
        if command.id == SET_COLOR:
            components = [args[k] for k in ("r", "g", "b", "w")]
            if not all(isinstance(c, int) for c in components):
                return ResultCode.INVALID_VALUE, b""  # 0xFF is "absent", not full (§3)
            rgbw = [int(c) for c in components]  # type: ignore[arg-type]
            if not any(rgbw):
                return ResultCode.INVALID_VALUE, b""  # dark is Level's (§5.8)
            for attribute, component in zip((RED, GREEN, BLUE, WHITE), rgbw, strict=True):
                if self.values[attribute] != component:
                    self.set(attribute, component)
            return ResultCode.OK, b""
        return ResultCode.UNSUPPORTED_ELEMENT, b""

    def _bounded(self, level: int) -> int:
        low, high = self.num(MIN_LEVEL), self.num(MAX_LEVEL)
        return max(min(low, high), min(level, high))

    def _level(self, level: int) -> None:
        """`Level` the only true state, `OnOff` from it, `LastLevel` never zero (§5.1)."""
        before = self.num(LEVEL)
        if level == before:
            return
        self.set(LAST_LEVEL, level or before, report=False)
        self.set(LEVEL, level)
        if (level > 0) != bool(self.values[ON_OFF]):
            self._switch(level > 0)

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
            if self.has(Feature.LEVEL):
                self._level(0)
            else:
                self.set(ON_OFF, False)
