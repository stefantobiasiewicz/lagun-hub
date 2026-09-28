"""Type 0x0001 Device — the node identity. Always on ep0."""

from __future__ import annotations

from typing import Final

from lagunhub.protocol.endpoints.definition import AttributeDef as A
from lagunhub.protocol.endpoints.definition import CommandDef as C
from lagunhub.protocol.endpoints.definition import EventDef as E
from lagunhub.protocol.endpoints.definition import Field as F
from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.registry import EndpointType

UID: Final = 0x00
SHORT_ID: Final = 0x01
DEVICE_TYPE: Final = 0x02
PROTOCOL_VERSION: Final = 0x03
FIRMWARE_VERSION: Final = 0x04
HARDWARE_VERSION: Final = 0x05
VENDOR_ID: Final = 0x06
ENDPOINT_COUNT: Final = 0x07
VENDOR_NAME: Final = 0x08
MODEL_NAME: Final = 0x09
UPTIME: Final = 0x10
BOOT_COUNT: Final = 0x11
LAST_RESET_REASON: Final = 0x12
ADDRESS: Final = 0x20

IDENTIFY: Final = 0x00
REBOOT: Final = 0x01
FACTORY_RESET: Final = 0x02
FACTORY_RESET_MAGIC: Final = 0x4C41474E  # the characters "LAGN"

BOOTED: Final = 0x01


class ResetReason:
    POWER_ON = 0
    EXTERNAL = 1
    WATCHDOG = 2
    SOFTWARE = 3
    BROWNOUT = 4
    UNKNOWN = 5


DEVICE: Final = TypeDef(
    type_id=EndpointType.Device,
    revision=1,
    attributes={
        UID: A(UID, "Uid", "blob", "RO", persist=True),
        SHORT_ID: A(SHORT_ID, "ShortId", "u32", "RO", persist=True),
        DEVICE_TYPE: A(DEVICE_TYPE, "DeviceType", "u16", "RO", persist=True),
        PROTOCOL_VERSION: A(PROTOCOL_VERSION, "ProtocolVersion", "u16", "RO", default=0x0001),
        FIRMWARE_VERSION: A(FIRMWARE_VERSION, "FirmwareVersion", "u32", "RO", default=0x0001_0000),
        HARDWARE_VERSION: A(
            HARDWARE_VERSION, "HardwareVersion", "u16", "RO", persist=True, default=1, optional=True
        ),
        VENDOR_ID: A(VENDOR_ID, "VendorId", "u16", "RO", persist=True),
        ENDPOINT_COUNT: A(ENDPOINT_COUNT, "EndpointCount", "u8", "RO"),
        VENDOR_NAME: A(VENDOR_NAME, "VendorName", "string", "RO", persist=True, optional=True),
        MODEL_NAME: A(MODEL_NAME, "ModelName", "string", "RO", persist=True, optional=True),
        UPTIME: A(UPTIME, "Uptime", "u32", "RO"),
        BOOT_COUNT: A(BOOT_COUNT, "BootCount", "u16", "RO", persist=True, optional=True),
        LAST_RESET_REASON: A(LAST_RESET_REASON, "LastResetReason", "enum8", "RO"),
        ADDRESS: A(ADDRESS, "Address", "u16", "RW", persist=True, hi=0x7FF),
    },
    commands={
        IDENTIFY: C(IDENTIFY, "Identify", (F("duration", "u16"),)),
        REBOOT: C(REBOOT, "Reboot", (F("delay", "u16"),)),
        FACTORY_RESET: C(FACTORY_RESET, "FactoryReset", (F("magic", "u32"),)),
    },
    events={
        BOOTED: E(
            BOOTED,
            "Booted",
            (F("resetReason", "enum8"), F("firmwareVersion", "u32")),
        ),
    },
)
