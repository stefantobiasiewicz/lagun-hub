"""Type 0x0004 Groups — the table of endpoint group memberships. Always on ep2."""

from __future__ import annotations

from typing import Final

from lagunhub.protocol.endpoints.definition import AttributeDef as A
from lagunhub.protocol.endpoints.definition import CommandDef as C
from lagunhub.protocol.endpoints.definition import Field as F
from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.registry import EndpointType

GROUP_CAPACITY: Final = 0x00
GROUP_COUNT: Final = 0x01

ADD_GROUP: Final = 0x00
REMOVE_GROUP: Final = 0x01
REMOVE_ALL_GROUPS: Final = 0x02
GET_MEMBERSHIP: Final = 0x03

MEMBERSHIP_PER_RESPONSE: Final = 3
"""The tightest case in the whole specification: a header, a counter and three group
numbers make **8 of 8 bytes**, without a single spare one
(05-mapowanie-can.md §5.3)."""


GROUPS: Final = TypeDef(
    type_id=EndpointType.Groups,
    revision=1,
    attributes={
        GROUP_CAPACITY: A(GROUP_CAPACITY, "GroupCapacity", "u8", "RO", persist=True),
        GROUP_COUNT: A(GROUP_COUNT, "GroupCount", "u8", "RO", persist=True, reported=True),
    },
    commands={
        ADD_GROUP: C(ADD_GROUP, "AddGroup", (F("endpoint", "u8"), F("group", "u16"))),
        REMOVE_GROUP: C(REMOVE_GROUP, "RemoveGroup", (F("endpoint", "u8"), F("group", "u16"))),
        REMOVE_ALL_GROUPS: C(REMOVE_ALL_GROUPS, "RemoveAllGroups", (F("endpoint", "u8"),)),
        GET_MEMBERSHIP: C(GET_MEMBERSHIP, "GetMembership", (F("endpoint", "u8"),)),
    },
)
