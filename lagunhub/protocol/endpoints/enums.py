"""Named attribute values — one table for the file, for the API and for the view.

`Purpose: LIGHT` in the configuration file and `Purpose: LIGHT` in an API response are
meant to mean the same thing and to come from the same place. Otherwise the front end
would show `1` where the file shows `LIGHT` — and a person would have to know those
are one and the same.

The table is **written out by hand**, like the rest of the type catalogue (open point
**H8**).
"""

from __future__ import annotations

from enum import Enum
from typing import Final

from lagunhub.protocol.endpoints.definition import AttributeDef
from lagunhub.protocol.values import Value

ENUMS: Final[dict[str, tuple[type, ...]]] = {}


def _build() -> None:
    from lagunhub.protocol.endpoints import key_input, output
    from lagunhub.protocol.quantities import Quantity

    ENUMS.update(
        {
            "Purpose": (output.Purpose,),
            "PowerOnBehaviour": (output.PowerOnBehaviour,),
            "EventMode": (key_input.EventMode,),
            "Quantity": (Quantity,),
        }
    )


_build()


def choices(meta: AttributeDef) -> tuple[str, ...]:
    """The names this attribute accepts. An empty tuple means "an ordinary number".

    Exposed through the API so that the front end need not know them independently —
    a drop-down list is to come from the hub, not from a table copied into JavaScript.
    """
    out: list[str] = []
    for collected in ENUMS.get(meta.name, ()):
        if isinstance(collected, type) and issubclass(collected, Enum):
            out.extend(member.name for member in collected)
        else:
            out.extend(name for name in vars(collected) if not name.startswith("_"))
    return tuple(out)


def coerce(meta: AttributeDef, raw: object) -> Value:
    """A value from a person -> a value on the wire. Raises ``ValueError``.

    A number passes through unchanged: the name is a convenience, not the only way.
    """
    if isinstance(raw, bool | int):
        return raw
    name = str(raw)
    for collected in ENUMS.get(meta.name, ()):
        if hasattr(collected, name):
            return int(getattr(collected, name))
    if meta.type == "bool":
        if name.lower() in ("true", "on", "yes", "1"):
            return True
        if name.lower() in ("false", "off", "no", "0"):
            return False
    try:
        return int(name, 0)
    except ValueError:
        pass
    familiar = ", ".join(choices(meta)) or "a number"
    raise ValueError(f"{meta.name} knows no value {name!r}; it accepts: {familiar}")


def render(meta: AttributeDef, value: Value) -> str | None:
    """The name of a value, if it has one. ``None`` for ordinary numbers."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    for collected in ENUMS.get(meta.name, ()):
        if isinstance(collected, type) and issubclass(collected, Enum):
            try:
                return str(collected(value).name)
            except ValueError:
                continue
        for name, number in vars(collected).items():
            if not name.startswith("_") and number == value:
                return name
    return None


__all__ = ["ENUMS", "choices", "coerce", "render"]
