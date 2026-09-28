"""The behaviour of the Scenes type. Definitions: lagunhub.protocol.endpoints.scenes.

It exists **if and only if the node has some output** (01-model-danych.md §1.2) — and
that rule is enforced by ``NodeCore``, not by this file.

The table is empty: the endpoint is present and refuses. The scenes' contents come
later — they are the one thing the hub cannot read out of a node,
so it needs attention while being written, not in passing.
"""

from __future__ import annotations

from typing import ClassVar

from lagunhub.protocol.endpoints.definition import CommandDef, TypeDef
from lagunhub.protocol.endpoints.scenes import SCENE_CAPACITY, SCENES
from lagunhub.protocol.registry import ResultCode
from lagunhub.protocol.values import Value
from tests.node.endpoints.base import SystemEndpoint


class Scenes(SystemEndpoint):
    TYPE: ClassVar[TypeDef] = SCENES

    def on_command(self, command: CommandDef, args: dict[str, Value]) -> tuple[ResultCode, bytes]:
        del command, args
        if self.num(SCENE_CAPACITY) == 0:
            return ResultCode.INSUFFICIENT_SPACE, b""
        return ResultCode.UNSUPPORTED_ELEMENT, b""
