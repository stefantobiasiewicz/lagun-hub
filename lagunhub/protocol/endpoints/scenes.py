"""Type 0x0003 Scenes — stored output states. On ep3, where a node has an output.

A scene is keyed by the **pair** ``(group, number)``, and group `0` means "all of the
node’s outputs" while not being an address — it appears in no frame at all.
"""

from __future__ import annotations

from typing import Final

from lagunhub.protocol.endpoints.definition import AttributeDef as A
from lagunhub.protocol.endpoints.definition import CommandDef as C
from lagunhub.protocol.endpoints.definition import Field as F
from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.registry import EndpointType

SCENE_CAPACITY: Final = 0x00
SCENE_COUNT: Final = 0x01
LAST_GROUP: Final = 0x02
LAST_SCENE: Final = 0x03

STORE: Final = 0x00
RECALL: Final = 0x01
REMOVE: Final = 0x02
REMOVE_ALL: Final = 0x03

SCENES: Final = TypeDef(
    type_id=EndpointType.Scenes,
    revision=1,
    attributes={
        SCENE_CAPACITY: A(SCENE_CAPACITY, "SceneCapacity", "u8", "RO", persist=True),
        SCENE_COUNT: A(SCENE_COUNT, "SceneCount", "u8", "RO", persist=True, reported=True),
        LAST_GROUP: A(LAST_GROUP, "LastGroup", "u16", "RO", reported=True, hi=2047, optional=True),
        LAST_SCENE: A(LAST_SCENE, "LastScene", "u8", "RO", reported=True, optional=True),
    },
    commands={
        STORE: C(STORE, "Store", (F("group", "u16", in_group_address=True), F("scene", "u8"))),
        RECALL: C(
            RECALL,
            "Recall",
            (
                F("group", "u16", in_group_address=True),
                F("scene", "u8"),
                F("transition", "u16"),
            ),
        ),
        REMOVE: C(REMOVE, "Remove", (F("group", "u16", in_group_address=True), F("scene", "u8"))),
        REMOVE_ALL: C(REMOVE_ALL, "RemoveAll", (F("group", "u16", in_group_address=True),)),
    },
)
