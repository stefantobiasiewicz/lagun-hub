"""The catalogue of endpoint types — the counterpart of
[`types/`](../../lagunbus-spec/types/).

One place, used by the hub and by whoever speaks to it as a node. Definitions:
numbers, types, ranges, command arguments. **There is no behaviour here** — what a node
does when a command arrives is the node's business, not the catalogue's.
"""

from __future__ import annotations

from typing import Final

from lagunhub.protocol.endpoints.common import COMMON
from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.endpoints.device import DEVICE
from lagunhub.protocol.endpoints.diagnostics import DIAGNOSTICS
from lagunhub.protocol.endpoints.display import DISPLAY
from lagunhub.protocol.endpoints.groups import GROUPS
from lagunhub.protocol.endpoints.key_input import KEY_INPUT
from lagunhub.protocol.endpoints.output import OUTPUT
from lagunhub.protocol.endpoints.scenes import SCENES
from lagunhub.protocol.endpoints.sense import SENSE
from lagunhub.protocol.endpoints.value_input import VALUE_INPUT
from lagunhub.protocol.registry import EndpointType

CATALOG: Final[dict[EndpointType, TypeDef]] = {
    definition.type_id: definition
    for definition in (
        DEVICE,
        DIAGNOSTICS,
        SCENES,
        GROUPS,
        KEY_INPUT,
        VALUE_INPUT,
        OUTPUT,
        DISPLAY,
        SENSE,
    )
}


def by_name(name: str) -> TypeDef:
    try:
        endpoint_type = EndpointType[name]
    except KeyError:
        raise ValueError(f"unknown endpoint type: {name!r}") from None
    definition = CATALOG.get(endpoint_type)
    if definition is None:
        familiar = ", ".join(sorted(t.name for t in CATALOG))
        raise ValueError(f"type {name} has no definition yet; described: {familiar}")
    return definition


__all__ = [
    "CATALOG",
    "COMMON",
    "DEVICE",
    "DIAGNOSTICS",
    "DISPLAY",
    "GROUPS",
    "KEY_INPUT",
    "OUTPUT",
    "SCENES",
    "SENSE",
    "VALUE_INPUT",
    "by_name",
]
