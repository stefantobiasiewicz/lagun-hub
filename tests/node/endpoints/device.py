"""The behaviour of the Device type. Definitions: lagunhub.protocol.endpoints.device."""

from __future__ import annotations

import zlib
from typing import ClassVar

from lagunhub.protocol.endpoints.definition import CommandDef, TypeDef
from lagunhub.protocol.endpoints.device import (
    ADDRESS,
    BOOT_COUNT,
    DEVICE,
    FACTORY_RESET,
    FACTORY_RESET_MAGIC,
    IDENTIFY,
    MODEL_NAME,
    REBOOT,
    UID,
    UPTIME,
    VENDOR_NAME,
)
from lagunhub.protocol.registry import ResultCode
from lagunhub.protocol.values import Value, encode, encode_blob, encode_string
from tests.node.endpoints.base import SystemEndpoint


def short_id(uid: bytes) -> int:
    """ShortId to CRC-32 z Uid (types/0x0001-device.md §2.1)."""
    return zlib.crc32(uid) & 0xFFFF_FFFF


class Device(SystemEndpoint):
    TYPE: ClassVar[TypeDef] = DEVICE

    uid: bytes = b""
    vendor_name: str = ""
    model_name: str = ""

    def read(self, attribute: int) -> tuple[ResultCode, bytes]:
        if attribute == UPTIME and self.node is not None:
            return ResultCode.OK, encode("u32", self.node.uptime)
        return super().read(attribute)

    def long_value(self, attribute: int) -> bytes | None:
        if attribute == UID:
            return encode_blob(self.uid)
        if attribute == VENDOR_NAME:
            return encode_string(self.vendor_name)
        if attribute == MODEL_NAME:
            return encode_string(self.model_name)
        return None

    def on_command(self, command: CommandDef, args: dict[str, Value]) -> tuple[ResultCode, bytes]:
        if command.id == IDENTIFY:
            # A node with nothing to signal with still has to accept the command
            # (§3.1).
            return ResultCode.OK, b""
        if command.id == REBOOT:
            if self.node is not None:
                self.node.request_reboot(int(args["delay"] or 0) / 1000.0)  # type: ignore[arg-type]
            return ResultCode.OK, b""
        if command.id == FACTORY_RESET:
            if args["magic"] != FACTORY_RESET_MAGIC:
                return ResultCode.INVALID_VALUE, b""
            if self.node is not None:
                self.node.factory_reset()
            return ResultCode.OK, b""
        return ResultCode.UNSUPPORTED_ELEMENT, b""

    def on_write(self, attribute: int, value: Value) -> None:
        if attribute == ADDRESS and self.node is not None:
            # The reply still travels from the old address, everything after it from the
            # new one (06-cykl-zycia-urzadzenia.md §4.2) — which is why the node defers
            # the change, not the endpoint.
            self.node.request_address_change(self.num(ADDRESS))

    def bump_boot_count(self) -> None:
        """BootCount is optional — a node that has none simply does not count."""
        if BOOT_COUNT in self.spec:
            self.values[BOOT_COUNT] = self.num(BOOT_COUNT) + 1
