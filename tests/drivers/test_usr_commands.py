from __future__ import annotations

import pytest

from lagunhub.drivers.base import Rejected
from lagunhub.drivers.usr_can315 import commands as at


def test_query_and_setting_lines() -> None:
    assert at.query(at.VERSION) == "AT+VER"
    assert at.query(at.TEST) == "AT"
    assert at.setting(at.CAN, 500, "0", at.FrameType.EXTENDED) == "AT+CAN=500,0,EDTF"
    assert at.setting(at.MESSAGE, True, False) == "AT+MSG=1,0"
    assert at.line("AT+VER") == b"AT+VER\r"


def test_parse_returns_fields() -> None:
    assert at.parse(b"\r\n+CAN:500,1,EDTF\r\n\r\nOK\r\n", at.CAN) == ("500", "1", "EDTF")


def test_parse_skips_the_echoed_command() -> None:
    raw = b"AT+CAN\r\n\r\n+CAN:500,1,EDTF\r\n\r\nOK\r\n"
    assert at.parse(raw, at.CAN) == ("500", "1", "EDTF")


def test_parse_bare_ok_has_no_fields() -> None:
    assert at.parse(b"\r\nOK\r\n", at.SAVE) == ()
    assert at.parse(b"\r\n+OK\r\n", at.SAVE) == ()


def test_parse_error_code_is_a_rejection() -> None:
    with pytest.raises(Rejected, match="bad parameter"):
        at.parse(b"\r\n+ERROR:4\r\n", at.CAN)


def test_parse_without_ok_is_a_rejection() -> None:
    with pytest.raises(Rejected, match="without OK"):
        at.parse(b"garbage", at.CAN)


def test_bitrate_must_be_on_the_device_list() -> None:
    assert at.bitrate_field(500_000) == "500"
    with pytest.raises(ValueError, match="not on the device list"):
        at.bitrate_field(333_000)


def test_custom_bitrate_is_read_but_not_set() -> None:
    assert at.read_bitrate("custom(666.66K)") == 666_660
    assert at.read_bitrate("500") == 500_000
    assert at.show_bitrate(666_660) == "666.66"


def test_fd_data_bitrate_has_its_own_range() -> None:
    assert at.fd_bitrate_field(2_000_000) == "2000"
    with pytest.raises(ValueError):
        at.fd_bitrate_field(50_000)


def test_can_id_goes_without_0x() -> None:
    assert at.can_id_field(0x1FF) == "1FF"
