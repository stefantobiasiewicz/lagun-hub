"""The behaviour of the Display type. Definitions: lagunhub.protocol.endpoints.display.

A field shows what it is written (types/0x0022-display.md §5): a `Value` write replaces a
text and a `Text` write a value, `Content` is reported when what it shows changes, and
content nobody renews expires. `Text` comes only as a LONG_WRITE, assembled by the node
core (`NodeCore._take_segment`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.endpoints.display import (
    CONTENT,
    CONTENT_TIMEOUT,
    DISPLAY,
    QUANTITY,
    TEXT,
    TEXT_LENGTH,
    VALUE,
    Content,
)
from lagunhub.protocol.quantities import Quantity
from lagunhub.protocol.registry import ResultCode
from lagunhub.protocol.values import ABSENT, Value
from tests.node.endpoints.base import FunctionalEndpoint

EMPTY = b"\x00"


@dataclass
class Display(FunctionalEndpoint):
    TYPE: ClassVar[TypeDef] = DISPLAY

    quantity: Quantity = Quantity.TEMPERATURE
    text_length: int = 16
    own_timeout: float = 600.0
    """The expiry of a field without `ContentTimeout` (§5.5)."""

    text: bytes = field(default=EMPTY, init=False)
    """`Text` with its length prefix."""
    written: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.values[QUANTITY] = int(self.quantity)
        self.values[TEXT_LENGTH] = self.text_length
        self.values[VALUE] = ABSENT
        self.values[CONTENT] = int(Content.NONE)

    @property
    def shows(self) -> Content:
        return Content(self.num(CONTENT))

    @property
    def shown_text(self) -> str:
        return self.text[1:].decode("utf-8", errors="replace")

    def long_value(self, attribute: int) -> bytes | None:
        return self.text if attribute == TEXT else None

    def on_write(self, attribute: int, value: Value) -> None:
        if attribute != VALUE:
            return
        self.text = EMPTY
        self._show(Content.NONE if value is ABSENT else Content.VALUE)

    def on_write_long(self, attribute: int, value: bytes) -> ResultCode:
        if attribute != TEXT or len(value) - 1 > self.num(TEXT_LENGTH):
            return ResultCode.INVALID_VALUE
        self.values[VALUE] = ABSENT
        self.text = value
        self._show(Content.TEXT if len(value) > 1 else Content.NONE)
        return ResultCode.OK

    def tick(self, now: float) -> None:
        limit = float(self.opt(CONTENT_TIMEOUT, 0)) if CONTENT_TIMEOUT in self.spec else None
        if limit is None:
            limit = self.own_timeout
        if self.shows is Content.NONE or not limit or now - self.written < limit:
            return
        self.values[VALUE] = ABSENT
        self.text = EMPTY
        self._show(Content.NONE)

    def _show(self, content: Content) -> None:
        if self.node is not None:
            self.written = self.node.clock()
        self.set(CONTENT, int(content))
