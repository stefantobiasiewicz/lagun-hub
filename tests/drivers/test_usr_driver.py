from __future__ import annotations

from dataclasses import replace

from lagunhub.drivers.base import CanSettings, Mode, configure
from lagunhub.drivers.usr_can315 import LAGUNBUS, UsrCan315
from lagunhub.drivers.usr_can315 import commands as at

from .fakes import FACTORY, LAGUNBUS_READY, FakeTransport


def driver(replies: dict[str, str], *, echo: bool = False) -> tuple[UsrCan315, FakeTransport]:
    transport = FakeTransport(replies, echo=echo)
    return UsrCan315(transport), transport


def test_read_factory_device() -> None:
    usr, _ = driver(FACTORY)
    settings = usr.read()
    assert settings.bitrate == 100_000
    assert not settings.extended
    assert settings.fd
    assert settings.conversion is at.Conversion.TRANS
    assert settings.address == "192.168.0.7"


def test_info() -> None:
    usr, _ = driver(FACTORY)
    info = usr.info()
    assert (info.model, info.serial, info.where) == ("USR-CAN315", "03500324092600069753", "fake")


def test_device_in_sync_has_an_empty_plan() -> None:
    usr, _ = driver(LAGUNBUS_READY)
    assert usr.plan(LAGUNBUS) == ()


def test_factory_device_plan() -> None:
    usr, _ = driver(FACTORY)
    assert [change.how for change in usr.plan(LAGUNBUS)] == [
        "AT+CAN=500,0,EDTF",
        "AT+CANFD=0,0,2000",
        "AT+TMODE=PROTOL",
        "AT+MSG=1,1",
    ]


def test_core_settings_get_lagunbus_conversion() -> None:
    """The hub may speak only of the bus; the box's own modes come from LAGUNBUS."""
    usr, _ = driver(FACTORY)
    assert "AT+TMODE=PROTOL" in [change.how for change in usr.plan(CanSettings())]


def test_fd_data_rate_is_left_alone_while_fd_is_off() -> None:
    replies = LAGUNBUS_READY | {"AT+CANFD": "\r\n+CANFD:0,0,2000\r\n\r\nOK\r\n"}
    usr, _ = driver(replies)
    assert usr.plan(LAGUNBUS) == ()


def test_custom_bitrate_does_not_break_the_plan() -> None:
    replies = LAGUNBUS_READY | {"AT+CAN": "\r\n+CAN:custom(666.66K),0,EDTF\r\n\r\nOK\r\n"}
    usr, _ = driver(replies, echo=True)
    (change,) = usr.plan(LAGUNBUS)
    assert (change.current, change.how) == ("666.66,0,EDTF", "AT+CAN=500,0,EDTF")


def test_listen_only_maps_to_silent() -> None:
    usr, _ = driver(LAGUNBUS_READY)
    (change,) = usr.plan(replace(LAGUNBUS, mode=Mode.LISTEN_ONLY))
    assert change.how == "AT+CANMODE=SILENT"


def test_network_changes_come_last() -> None:
    usr, _ = driver(FACTORY)
    wanted = replace(
        LAGUNBUS, address="192.168.1.31", gateway="192.168.1.1", socket_host="192.168.1.31"
    )
    commands = [change.how for change in usr.plan(wanted)]
    assert commands[-2:] == [
        "AT+WANN=STATIC,192.168.1.31,255.255.255.0,192.168.1.1",
        "AT+SOCKA=TCPS,192.168.1.31,20001",
    ]


def test_dhcp_needs_no_address() -> None:
    usr, _ = driver(LAGUNBUS_READY)
    (change,) = usr.plan(replace(LAGUNBUS, ip_mode=at.IpMode.DHCP))
    assert change.how == "AT+WANN=DHCP"


def test_configure_changes_then_saves_then_restarts() -> None:
    usr, transport = driver(FACTORY)
    assert configure(usr, LAGUNBUS) == ()
    assert transport.sent[-6:] == [
        "AT+CAN=500,0,EDTF",
        "AT+CANFD=0,0,2000",
        "AT+TMODE=PROTOL",
        "AT+MSG=1,1",
        "AT+CFGTF",
        "AT+Z",
    ]


def test_configure_dry_run_touches_nothing() -> None:
    usr, transport = driver(FACTORY)
    assert len(configure(usr, LAGUNBUS, dry_run=True)) == 4
    assert transport.assignments == []
