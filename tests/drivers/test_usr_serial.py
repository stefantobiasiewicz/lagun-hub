"""The serial transport against a pseudo-terminal pretending to be the adapter."""

from __future__ import annotations

import os
import pty
import threading
from collections.abc import Iterator

import pytest

from lagunhub.drivers.base import Unreachable, configure
from lagunhub.drivers.usr_can315 import LAGUNBUS, SerialAt, UsrCan315

from .fakes import FACTORY


def adapter(fd: int, log: list[str], *, already_in_at_mode: bool) -> None:
    """``+++`` → ``A``, ``a`` → ``+OK``, then AT commands. Silent outside AT mode."""
    buffer = b""
    in_at_mode = already_in_at_mode
    while True:
        try:
            chunk = os.read(fd, 1024)
        except OSError:
            return
        if not chunk:
            return
        buffer += chunk
        if buffer == b"+++":
            buffer = b""
            if not already_in_at_mode:
                os.write(fd, b"A")
            continue
        if buffer == b"a":
            buffer = b""
            in_at_mode = True
            os.write(fd, b"\r\n+OK\r\n")
            continue
        if buffer.endswith(b"\r"):
            command = buffer[:-1].decode()
            buffer = b""
            log.append(command)
            if in_at_mode:
                os.write(fd, FACTORY.get(command, "\r\nOK\r\n").encode())


def start(already_in_at_mode: bool = False) -> tuple[str, list[str]]:
    master, slave = pty.openpty()
    log: list[str] = []
    threading.Thread(
        target=adapter,
        args=(master, log),
        kwargs={"already_in_at_mode": already_in_at_mode},
        daemon=True,
    ).start()
    return os.ttyname(slave), log


@pytest.fixture
def factory_adapter() -> Iterator[tuple[str, list[str]]]:
    yield start()


def test_handshake_and_configure(factory_adapter: tuple[str, list[str]]) -> None:
    path, log = factory_adapter
    usr = UsrCan315(SerialAt(path, timeout=0.5))
    usr.open()
    assert configure(usr, LAGUNBUS) == ()
    assert log[-2:] == ["AT+CFGTF", "AT+Z"]


def test_adapter_already_in_command_mode() -> None:
    path, log = start(already_in_at_mode=True)
    usr = UsrCan315(SerialAt(path, timeout=0.3))
    usr.open()
    assert log[0] == "AT"
    assert usr.read().bitrate == 100_000


def test_silent_port_is_unreachable() -> None:
    _, slave = pty.openpty()
    with pytest.raises(Unreachable, match="did not enter AT command mode"):
        SerialAt(os.ttyname(slave), timeout=0.2).open()


def test_missing_port_is_unreachable() -> None:
    with pytest.raises(Unreachable, match="cannot open"):
        SerialAt("/dev/no-such-port").open()
