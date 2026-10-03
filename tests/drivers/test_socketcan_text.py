"""The text layer: ``ip -details -json`` output and the ``ip`` commands."""

from __future__ import annotations

import json

import pytest

from lagunhub.drivers.base import DriverError, Mode
from lagunhub.drivers.socketcan import iproute

from .fakes import link_json


def test_parse_a_configured_link() -> None:
    link = iproute.parse(link_json(ctrlmode=["FD", "LISTEN-ONLY"]), "can0")
    assert (link.up, link.bitrate, link.restart_ms, link.clock) == (True, 500_000, 100, 40_000_000)
    assert link.fd
    assert link.mode is Mode.LISTEN_ONLY


def test_never_configured_link_reads_as_unset() -> None:
    text = json.dumps(
        [
            {
                "ifname": "can0",
                "flags": ["NOARP", "ECHO"],
                "txqlen": 10,
                "linkinfo": {
                    "info_kind": "can",
                    "info_data": {"state": "STOPPED", "clock": 40000000},
                },
            }
        ]
    )
    link = iproute.parse(text, "can0")
    assert (link.up, link.bitrate, link.restart_ms) == (False, 0, 0)


def test_not_can_is_refused() -> None:
    text = json.dumps([{"ifname": "eth0", "link_type": "ether"}])
    with pytest.raises(DriverError, match="not a CAN controller"):
        iproute.parse(text, "eth0")


def test_mode_options_set_both_flags() -> None:
    assert iproute.mode_options(Mode.NORMAL) == ("listen-only", "off", "loopback", "off")
    assert iproute.mode_options(Mode.LOOPBACK) == ("listen-only", "off", "loopback", "on")


def test_virtual_link_has_no_timing() -> None:
    text = json.dumps(
        [{"ifname": "vcan0", "flags": ["NOARP"], "txqlen": 1000, "linkinfo": {"info_kind": "vcan"}}]
    )
    link = iproute.parse(text, "vcan0")
    assert link.virtual
    assert (link.up, link.bitrate) == (False, 0)
