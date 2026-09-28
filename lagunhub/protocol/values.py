"""Scalar value codec — lagunbus-spec/02-typy-danych.md.

Two rules from there hold without exception: **everything is little-endian** and
**there are no floating-point numbers**. Every physical quantity is an integer with a
fixed scale.

Strings, arrays and physical-quantity scales do not fit in a frame and need segments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


class _Absent:
    """The "absent" value — a sensor has not initialised, or does not know
    (02-typy-danych.md §2.1).

    It is a thing of its own rather than a ``None`` pretending to be a number: the
    alternative, reporting zero as "I do not know", leads to a thermostat heating a
    room
    do −273 °C.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "ABSENT"

    def __bool__(self) -> bool:
        return False


ABSENT: Final = _Absent()

Value = int | bool | _Absent


@dataclass(frozen=True, slots=True)
class Scalar:
    size: int
    signed: bool
    absent: int | None
    """``None`` for bit fields — no bits set is a valid state."""

    @property
    def lo(self) -> int:
        if not self.signed:
            return 0
        return -(1 << (self.size * 8 - 1)) + 1

    @property
    def hi(self) -> int:
        if self.signed:
            return (1 << (self.size * 8 - 1)) - 1
        return (1 << (self.size * 8)) - (2 if self.absent is not None else 1)


SCALARS: Final[dict[str, Scalar]] = {
    "bool": Scalar(1, False, 0xFF),
    "u8": Scalar(1, False, 0xFF),
    "u16": Scalar(2, False, 0xFFFF),
    "u32": Scalar(4, False, 0xFFFF_FFFF),
    "i8": Scalar(1, True, -0x80),
    "i16": Scalar(2, True, -0x8000),
    "i32": Scalar(4, True, -0x8000_0000),
    "enum8": Scalar(1, False, 0xFF),
    "enum16": Scalar(2, False, 0xFFFF),
    "map8": Scalar(1, False, None),
    "map16": Scalar(2, False, None),
    "map32": Scalar(4, False, None),
}


def size_of(type_name: str) -> int:
    """Length in bytes. Strings and blobs have none — they do not fit in a frame."""
    if type_name not in SCALARS:
        raise ValueError(f"{type_name} has a variable length; use encode_string/encode_blob")
    return SCALARS[type_name].size


def in_range(type_name: str, value: Value) -> bool:
    """Whether a value fits the type’s range. "Absent" always fits."""
    if isinstance(value, _Absent):
        return SCALARS[type_name].absent is not None
    spec = SCALARS[type_name]
    if type_name == "bool":
        return value in (0, 1, True, False)
    return spec.lo <= int(value) <= spec.hi


def encode(type_name: str, value: Value) -> bytes:
    spec = SCALARS[type_name]
    if isinstance(value, _Absent):
        if spec.absent is None:
            raise ValueError(f'{type_name} has no "absent" value')
        raw = spec.absent
    else:
        raw = int(value)
    return raw.to_bytes(spec.size, "little", signed=spec.signed and raw < 0)


def decode(type_name: str, data: bytes) -> Value:
    spec = SCALARS[type_name]
    if len(data) != spec.size:
        raise ValueError(f"{type_name} needs {spec.size} B, got {len(data)}")
    raw = int.from_bytes(data, "little", signed=spec.signed)
    if spec.absent is not None and raw == spec.absent:
        return ABSENT
    if type_name == "bool":
        return bool(raw)
    return raw


# --- values longer than a frame, 02-typy-danych.md §2.2 -------------------------
#
# Strings and binary data NEVER fit into a single frame — they are sent in segments
# (05-mapowanie-can.md §6). Here the codec assembles the value itself with its length
# prefix; slicing it into segments is a separate layer's business.

STRING_PREFIX: Final = 1
BLOB_PREFIX: Final = 2


def encode_string(text: str) -> bytes:
    """UTF-8 with a ``u8`` length prefix. Non-ASCII characters work."""
    raw = text.encode("utf-8")
    if len(raw) > 255:
        raise ValueError(f"string longer than 255 B: {len(raw)}")
    return bytes([len(raw)]) + raw


def decode_string(data: bytes) -> str:
    if not data:
        raise ValueError("the string length prefix is missing")
    length = data[0]
    if len(data) < STRING_PREFIX + length:
        raise ValueError(f"string truncated: the prefix says {length}, there is {len(data) - 1}")
    return data[STRING_PREFIX : STRING_PREFIX + length].decode("utf-8")


def encode_blob(raw: bytes) -> bytes:
    """Binary data with a ``u16`` length prefix."""
    if len(raw) > 65535:
        raise ValueError(f"blob longer than 65535 B: {len(raw)}")
    return len(raw).to_bytes(2, "little") + raw


def decode_blob(data: bytes) -> bytes:
    if len(data) < BLOB_PREFIX:
        raise ValueError("the blob length prefix is missing")
    length = int.from_bytes(data[:BLOB_PREFIX], "little")
    if len(data) < BLOB_PREFIX + length:
        raise ValueError(
            f"blob truncated: the prefix says {length}, there is {len(data) - BLOB_PREFIX}"
        )
    return data[BLOB_PREFIX : BLOB_PREFIX + length]
