"""Type 0x0022 Display — one field of a display (types/0x0022-display.md).

A field **shows what it is written**: a number of its `Quantity` or a text. It measures
nothing and decides nothing, so it has no commands and no events; what it shows is state,
and state is set by a write (§3). `Content` is the one attribute it reports — it changes
without a write when the content expires (§5.5).

`Value` is typed by `Quantity`, as in `Sense`, and `Quantity` is the device's, read-only
(zadania/zrobione/FEAT-12.md, N3). `Text` is a `string`, so it always travels in segments — a
`LONG_WRITE`, never a `WRITE`, however short (02-typy-danych.md §2.2); the hub decides
the form by the attribute's type in `core/hub.py`.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final

from lagunhub.protocol.endpoints.definition import AttributeDef as A
from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.registry import EndpointType

CONTENT: Final = 0x00
QUANTITY: Final = 0x01
VALUE: Final = 0x02
TEXT_LENGTH: Final = 0x03
TEXT: Final = 0x04
CONTENT_TIMEOUT: Final = 0x10


class Feature:
    VALUE = 1 << 0
    TEXT = 1 << 1


class Content(IntEnum):
    """What the field shows now (§2.3)."""

    NONE = 0
    VALUE = 1
    TEXT = 2


DISPLAY: Final = TypeDef(
    type_id=EndpointType.Display,
    revision=1,
    attributes={
        CONTENT: A(CONTENT, "Content", "enum8", "RO", reported=True),
        QUANTITY: A(QUANTITY, "Quantity", "enum8", "RO", persist=True, feature=Feature.VALUE),
        VALUE: A(VALUE, "Value", "<Q>", "RW", feature=Feature.VALUE),
        TEXT_LENGTH: A(
            TEXT_LENGTH, "TextLength", "u8", "RO", persist=True, lo=1, hi=254, feature=Feature.TEXT
        ),
        TEXT: A(TEXT, "Text", "string", "RW", feature=Feature.TEXT),
        # 0 = never, otherwise 60–3600 s. Not a plain range, so the node checks it and the
        # hub sends what it is given; the file's value is the person's (§2.2).
        CONTENT_TIMEOUT: A(
            CONTENT_TIMEOUT,
            "ContentTimeout",
            "u16",
            "RW",
            persist=True,
            default=600,
            hi=3600,
            optional=True,
        ),
    },
)
