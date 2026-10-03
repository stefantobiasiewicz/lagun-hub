"""Driver for the USR-CAN315 converter — AT commands by two roads.

**Over the network** (``NetworkAt``): AT commands over UDP on port 48899, after sending
the keyword (``www.usr.cn`` from the factory). Both can be changed with ``AT+SEARCH``,
so both are parameters rather than constants in the code. This is how an adapter already
working in an installation is configured.

**Over the cable** (``SerialAt``): the USB-C socket on the side, AT commands after
entering command mode with ``+++`` → ``A`` → ``a`` → ``+OK``. The CAN315 manual **does
not promise this** — it describes USB-C as the socket for firmware upgrades, and
describes AT over a cable for the CAN316 — so the transport is here, but until someone
checks it against real hardware the network stays the sure road. The cable is still the
only way in when an adapter has lost its address or sits on a network you cannot reach.

That is exactly why the transport is a separate notion from the driver: the same set of
commands reaches the device by two completely different roads, and nothing above changes.

**The order of changes is not accidental.** CAN first, the network last: ``AT+WANN``
moves the device to a different address and the next question goes into the void. The
driver does not pretend otherwise — it says so plainly at ``apply``.
"""

from __future__ import annotations

import os
import select
import socket
import termios
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from ..base import (
    AdapterInfo,
    CanSettings,
    Change,
    DriverError,
    Mode,
    Unreachable,
    configure,
)
from . import commands as at

MODEL: Final = "USR-CAN315"

_MODES: Final = {
    Mode.NORMAL: at.CanMode.NORMAL,
    Mode.LISTEN_ONLY: at.CanMode.SILENT,
    Mode.LOOPBACK: at.CanMode.LOOPBACK,
}
_MODES_BACK: Final = {value: key for key, value in _MODES.items()}


# --- transport -------------------------------------------------------------------


@runtime_checkable
class Transport(Protocol):
    """A road to the device. One question, one answer, no interpretation."""

    where: str
    """Where it leads — address with port, serial port path. For printing."""

    def open(self) -> None: ...

    def request(self, command: str) -> bytes:
        """Send an AT line and return the raw answer. Raises ``Unreachable``."""
        ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Found:
    """A device that answered the broadcast."""

    address: str
    mac: str = ""
    name: str = ""

    @property
    def identity(self) -> str:
        """What this device **is**, not where it stands.

        An address is a route, not an identity — the same rule as for nodes
        (``core/config/model.py``). Here it has a bitter, concrete reason: every adapter
        leaves the box at one and the same ``192.168.0.7``, so keying on the address
        would glue two different devices into one. A MAC does not.
        """
        return self.mac or self.address


def discover(
    *,
    port: int = at.AT_PORT,
    keyword: str = at.AT_KEYWORD,
    timeout: float = 1.0,
    broadcast: str = "255.255.255.255",
    bind: str = "",
) -> tuple[Found, ...]:
    """Broadcast search for adapters within a network segment.

    It works like this: **the keyword alone** (``www.usr.cn``) goes out to port 48899,
    and every adapter that hears it answers with a line ``IP,MAC,name``. No ``AT`` —
    commands start only once it is known who to send them to.

    This is the only way to find an adapter that **cannot be sent anything**: it leaves
    the box at ``192.168.0.7``, so on a ``192.168.1.x`` network it answers nothing
    addressed to it. A broadcast travels one layer down and arrives regardless.

    ``bind`` picks the interface the broadcast leaves through — the local address of the
    card. A laptop on Wi-Fi and cable at once has two, and without being told it usually
    picks the wrong one; that is the most common cause of "found nothing" with the
    adapter lying right there.
    """
    answers: dict[str, Found] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if bind:
            sock.bind((bind, 0))
        sock.settimeout(timeout)
        sock.sendto(keyword.encode("ascii"), (broadcast, port))
        while True:
            try:
                raw, sender = sock.recvfrom(1024)
            except TimeoutError:
                break
            fields = raw.decode("ascii", errors="replace").strip().split(",")
            found = Found(fields[0], fields[1], fields[2]) if len(fields) >= 3 else Found(sender[0])
            # The device answers one broadcast more than once, so answers are deduplicated
            # by MAC, not by address: two factory-fresh adapters share 192.168.0.7 and
            # keying on the address would hide one of them.
            answers.setdefault(found.identity, found)
    return tuple(answers.values())


def conflicts(found: Sequence[Found]) -> tuple[tuple[Found, ...], ...]:
    """Groups of devices standing at **one** address. Empty means "you may work".

    Every adapter leaves the box at ``192.168.0.7``, so a second one plugged into the
    same network is not a second device but an **address conflict**. AT commands then go
    to both at once, both answers come back through the same socket and the driver reads
    whichever arrives first — so the wrong box gets configured, with nothing to show
    that it happened.

    There is one way out and it is boring: **one at a time**. Power up one adapter, give
    it an address, put it aside, power up the next. Or configure each over the USB-C
    cable, where the question "which one is it" never comes up.
    """
    by_address: dict[str, dict[str, Found]] = {}
    for device in found:
        # Keyed by identity inside the address, so the same device answering twice is
        # one device — a repeated answer is normal, two MACs on one address is not.
        by_address.setdefault(device.address, {})[device.identity] = device
    return tuple(tuple(group.values()) for group in by_address.values() if len(group) > 1)


def _is_at_reply(raw: bytes) -> bool:
    """Is this an answer to an AT command, or a leftover from the search?

    An AT answer always carries ``OK``, ``ERROR`` or a ``+NAME:`` line. An answer to the
    broadcast is three comma-separated fields and none of those.
    """
    text = raw.decode("ascii", errors="replace")
    return "OK" in text or "ERROR" in text or "+" in text


class NetworkAt:
    """AT commands over UDP. Satisfies ``Transport``.

    Configuration mode **closes itself** after a moment of silence, so the keyword is
    sent not once per session but every time an answer fails to arrive. Repeating is
    safe here: AT commands are questions or assignments, and both may be repeated.
    """

    def __init__(
        self,
        host: str,
        *,
        port: int = at.AT_PORT,
        keyword: str = at.AT_KEYWORD,
        timeout: float = 1.0,
        retries: int = 3,
    ) -> None:
        self.host = host
        self.port = port
        self.where = f"{host}:{port}"
        self.keyword = keyword
        self.timeout = timeout
        self.retries = retries
        self._sock: socket.socket | None = None

    def open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        self._sock = sock
        self._greet()

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _greet(self) -> None:
        """The keyword that opens network AT command mode."""
        sock = self._socket()
        sock.sendto(self.keyword.encode("ascii"), (self.host, self.port))
        try:
            sock.recvfrom(1024)
        except TimeoutError as exc:
            raise Unreachable(f"{self.host}:{self.port} did not answer the AT keyword") from exc

    def request(self, command: str) -> bytes:
        """One question, one answer — and only the answer to **this** question.

        The socket has to be drained before sending and sifted after receiving, because
        answers to the broadcast (``IP,MAC,name``) land on this same port. An adapter
        answers those several times, and on a network with several adapters every one of
        them answers — without sifting, the first such leftover would reach the parser as
        the answer to ``AT+CAN`` and the driver would decide the device talks nonsense.
        """
        sock = self._socket()
        payload = at.line(command)
        for attempt in range(self.retries):
            self._drain()
            sock.sendto(payload, (self.host, self.port))
            deadline = time.monotonic() + self.timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                sock.settimeout(left)
                try:
                    raw, _ = sock.recvfrom(2048)
                except TimeoutError:
                    break
                if _is_at_reply(raw):
                    sock.settimeout(self.timeout)
                    return raw
            sock.settimeout(self.timeout)
            # Silence usually means the command window closed, not a dead device.
            if attempt + 1 < self.retries:
                self._greet()
        raise Unreachable(f"{self.host}:{self.port} did not answer {command!r}")

    def _drain(self) -> None:
        """Throw away what is waiting in the socket. Someone else's answer is not ours."""
        sock = self._socket()
        sock.setblocking(False)
        try:
            while True:
                try:
                    sock.recvfrom(2048)
                except (BlockingIOError, TimeoutError):
                    return
        finally:
            sock.settimeout(self.timeout)

    def _socket(self) -> socket.socket:
        if self._sock is None:
            raise Unreachable("transport is closed; call open() first")
        return self._sock


class SerialAt:
    """AT commands over a cable. Satisfies ``Transport``.

    Without ``pyserial``: a serial port is ``termios`` and a descriptor, and the hub does
    not add a dependency where the standard library is enough — the same decision as in
    ``SocketCanBus``, where the ``AF_CAN`` socket is written out directly instead of
    pulling in ``python-can``.

    The default port settings are the factory ones: 115200, 8 bits, no parity, 1 stop bit.
    """

    def __init__(
        self,
        path: str,
        *,
        baudrate: int = 115_200,
        timeout: float = 1.0,
        retries: int = 3,
    ) -> None:
        self.path = path
        self.baudrate = baudrate
        self.timeout = timeout
        self.retries = retries
        self.where = path
        self._fd: int | None = None

    def open(self) -> None:
        if self.baudrate not in _BAUDS:
            allowed = ", ".join(str(value) for value in sorted(_BAUDS))
            raise DriverError(f"unsupported baudrate {self.baudrate}; allowed: {allowed}")
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise Unreachable(f"cannot open {self.path}: {exc}") from exc
        self._setup()
        self._enter_command_mode()

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _setup(self) -> None:
        """A raw port: no character processing in either direction.

        Without it the terminal driver turns ``CR`` into ``LF``, swallows ``0x11``/``0x13``
        as flow control and echoes everything back — and AT commands end in exactly that
        ``CR``, while that echo is exactly what we later look past in the answer.
        """
        fd = self._descriptor()
        speed = _BAUDS[self.baudrate]
        control: list[bytes | int] = list(termios.tcgetattr(fd)[6])
        control[termios.VMIN] = 0
        control[termios.VTIME] = 0
        # iflag, oflag and lflag all zero: no translation, no flow control, no echo.
        mode: list[int | list[bytes | int]] = [
            0,
            0,
            termios.CS8 | termios.CREAD | termios.CLOCAL,
            0,
            speed,
            speed,
            control,
        ]
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        termios.tcflush(fd, termios.TCIOFLUSH)

    def _enter_command_mode(self) -> None:
        """The ``+++`` → ``A`` → ``a`` → ``+OK`` sequence.

        Two things that are easy to miss and without which this does not work: ``+++``
        goes **without** ``CR``, and nothing may be sent for one packing interval before
        it — otherwise it lands inside the data and travels onto the bus as payload. The
        ``A`` that comes back has to be answered with ``a`` within 3 seconds.

        When the adapter is already in command mode, ``+++`` gets no answer at all — then
        we ask with a plain ``AT``. Only silence to both means there is nobody to talk to.
        """
        fd = self._descriptor()
        termios.tcflush(fd, termios.TCIOFLUSH)
        time.sleep(_PACKING_PAUSE)
        self._write(at.ENTER_COMMAND.encode("ascii"))
        if "A" in self._read(self.timeout):
            self._write(at.ENTER_ACK.encode("ascii"))
            if "OK" in self._read(self.timeout):
                return
        self._write(at.line(at.query(at.TEST)))
        if "OK" in self._read(self.timeout):
            return
        raise Unreachable(f"{self.path} did not enter AT command mode")

    def request(self, command: str) -> bytes:
        payload = at.line(command)
        for attempt in range(self.retries):
            self._write(payload)
            reply = self._read(self.timeout)
            if reply:
                return reply.encode("ascii", errors="replace")
            # Silence usually means the adapter dropped back to pass-through.
            if attempt + 1 < self.retries:
                self._enter_command_mode()
        raise Unreachable(f"{self.path} did not answer {command!r}")

    def _write(self, payload: bytes) -> None:
        fd = self._descriptor()
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])

    def _read(self, timeout: float) -> str:
        """Read until the answer ends, or until silence.

        The end shows up as ``OK`` or ``ERROR`` — but only on a **complete** line, since
        ``OK`` also appears inside an echoed command. When neither arrives, silence ends
        it: a port that went quiet is an answer in itself.
        """
        fd = self._descriptor()
        deadline = time.monotonic() + timeout
        buffer = bytearray()
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([fd], [], [], min(left, _QUIET))
            if not ready:
                if buffer:
                    break  # Quiet after something arrived: the answer is complete.
                continue
            chunk = os.read(fd, 1024)
            if not chunk:
                continue
            buffer += chunk
            deadline = time.monotonic() + _QUIET
        return buffer.decode("ascii", errors="replace")

    def _descriptor(self) -> int:
        if self._fd is None:
            raise Unreachable("transport is closed; call open() first")
        return self._fd


_BAUDS: Final = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115_200: termios.B115200,
    230_400: termios.B230400,
}

_PACKING_PAUSE: Final = 0.3
"""Silence before ``+++``. The adapter recognises the sequence only on a packet
boundary — a few hundred milliseconds of quiet are cheaper than one command let onto the
bus as data."""

_QUIET: Final = 0.05
"""The gap after which an answer counts as complete."""


# --- settings --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UsrSettings(CanSettings):
    """CAN315 configuration: the ``CanSettings`` core plus what exists nowhere else.

    The defaults describe an adapter **for LagunBus**, not an adapter out of the box —
    the ready-made set sits below as ``LAGUNBUS``.
    """

    can_id: int = 0
    """The fixed frame identifier in transparent mode. Unused in ``PROTOL``, but
    ``AT+CAN`` demands it anyway — the device sets the whole triple at once."""
    fd_bitrate: int = 100_000
    """CAN FD data-phase bitrate. Meaningless with ``fd=False``, and ``AT+CANFD`` wants a
    third parameter regardless."""
    conversion: at.Conversion = at.Conversion.PROTOL
    direction: at.Direction = at.Direction.BOTH
    frame_id: bool = True
    frame_info: bool = True
    ip_mode: at.IpMode = at.IpMode.STATIC
    address: str = ""
    mask: str = "255.255.255.0"
    gateway: str = ""
    socket_mode: at.SocketMode = at.SocketMode.TCP_SERVER
    socket_host: str = ""
    """In the server role — its own address; in the client role — the hub's address."""
    socket_port: int = 20001


LAGUNBUS: Final = UsrSettings(
    bitrate=500_000,
    extended=True,
    fd=False,
    mode=Mode.NORMAL,
    conversion=at.Conversion.PROTOL,
    frame_id=True,
    frame_info=True,
)
"""The adapter set up so the hub does not care that it sits behind Ethernet.

Two entries here matter more than the rest, and it is worth knowing why:

* **``PROTOL``, not ``TRANS``.** Transparent mode sends everything under one identifier —
  and in LagunBus **the whole addressing lives in the identifier**
  (``05-mapowanie-can.md``). An adapter in ``TRANS`` delivers the bytes and loses the one
  thing that mattered.
* **``frame_id`` and ``frame_info``.** Without them the frame on the network side carries
  neither its identifier nor its length — the hub gets payload with no sender.

The rest is LagunBus from ``05-mapowanie-can.md``: CAN 2.0B, 500 kbit/s, extended frames,
CAN FD off. The network — address, socket, port — stays empty, because that is a decision
of the installation, not of the protocol.
"""


# --- driver ----------------------------------------------------------------------


class UsrCan315:
    """A driver for one converter. Satisfies ``AdapterDriver``."""

    model = MODEL

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def open(self) -> None:
        self._transport.open()

    def close(self) -> None:
        """Close the session.

        ``AT+ENTM`` returns to working mode. Without it the adapter stays in AT mode for
        a while longer and **passes no frames** — so the bus goes quiet for a reason that
        cannot be seen on the bus.
        """
        try:
            self._ask(at.EXIT)
        except DriverError:
            pass  # Already gone or rebooting: nothing left to close politely.
        finally:
            self._transport.close()

    def __enter__(self) -> UsrCan315:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def info(self) -> AdapterInfo:
        name = self._one(at.NAME) or MODEL
        return AdapterInfo(
            model=name,
            firmware=self._one(at.VERSION),
            serial=self._one(at.SERIAL),
            where=self._transport.where,
        )

    def read(self) -> UsrSettings:
        """The whole configuration, one question per command.

        One question per command rather than one per parameter — the device hands back
        whole triples anyway, and every question is another datagram and another chance
        of silence.
        """
        can = self._ask(at.CAN)
        canfd = self._ask(at.CANFD)
        can_mode = self._ask(at.CAN_MODE)
        conversion = self._ask(at.CONVERSION)
        direction = self._ask(at.DIRECTION)
        message = self._ask(at.MESSAGE)
        wan = self._ask(at.WAN)
        sock = self._ask(at.SOCKET_A)

        return UsrSettings(
            bitrate=at.read_bitrate(can[0]),
            extended=can[2] == at.FrameType.EXTENDED.value,
            fd=canfd[0] == "1",
            mode=_MODES_BACK[at.CanMode(can_mode[0])],
            can_id=int(can[1], 16),
            fd_bitrate=at.read_bitrate(canfd[2]),
            conversion=at.Conversion(conversion[0]),
            direction=at.Direction(direction[0]),
            frame_id=message[0] == "1",
            frame_info=message[1] == "1",
            ip_mode=at.IpMode(wan[0]),
            address=wan[1] if len(wan) > 1 else "",
            mask=wan[2] if len(wan) > 2 else "",
            gateway=wan[3] if len(wan) > 3 else "",
            socket_mode=at.SocketMode(sock[0]),
            socket_host=sock[1],
            socket_port=int(sock[2]),
        )

    def plan(self, desired: CanSettings) -> tuple[Change, ...]:
        """What needs changing, in the order in which it may be done.

        The network **last**: ``AT+WANN`` moves the device to a different address, and
        then the next command has nobody to go to. With the CAN changes first, losing
        contact leaves an unfinished plan at worst, not a half-configured adapter.

        An empty ``address`` (and ``socket_host``) means "leave it as it is" — the
        LagunBus configuration speaks about the bus, while the network address is a
        matter for the installation.
        """
        wanted = _as_usr(desired)
        current = self.read()
        changes: list[Change] = []

        self._compare(
            changes,
            "CAN bitrate, id, frame type",
            at.CAN,
            (
                at.show_bitrate(current.bitrate),
                at.can_id_field(current.can_id),
                at.frame_type(current.extended).value,
            ),
            (
                at.bitrate_field(wanted.bitrate),
                at.can_id_field(wanted.can_id),
                at.frame_type(wanted.extended).value,
            ),
        )
        self._compare(
            changes,
            "CAN FD",
            at.CANFD,
            (
                "1" if current.fd else "0",
                "1" if current.fd else "0",
                at.show_bitrate(current.fd_bitrate),
            ),
            (
                "1" if wanted.fd else "0",
                "1" if wanted.fd else "0",
                _fd_data_field(current, wanted),
            ),
        )
        self._compare(
            changes,
            "controller mode",
            at.CAN_MODE,
            (_MODES[current.mode].value,),
            (_MODES[wanted.mode].value,),
        )
        self._compare(
            changes,
            "conversion mode",
            at.CONVERSION,
            (current.conversion.value,),
            (wanted.conversion.value,),
        )
        self._compare(
            changes,
            "conversion direction",
            at.DIRECTION,
            (current.direction.value,),
            (wanted.direction.value,),
        )
        self._compare(
            changes,
            "frame id and frame info in the payload",
            at.MESSAGE,
            ("1" if current.frame_id else "0", "1" if current.frame_info else "0"),
            ("1" if wanted.frame_id else "0", "1" if wanted.frame_info else "0"),
        )

        if wanted.ip_mode is at.IpMode.DHCP:
            # DHCP takes no address, mask or gateway: the router hands all three over.
            # The firmware also accepts AT+DHCPEN=ON for this; AT+WANN is the documented
            # way to say it in one command and leaves no room for the two to disagree.
            self._compare(
                changes,
                "network address",
                at.WAN,
                (current.ip_mode.value,),
                (at.IpMode.DHCP.value,),
            )
        elif wanted.address:
            self._compare(
                changes,
                "network address",
                at.WAN,
                (current.ip_mode.value, current.address, current.mask, current.gateway),
                (wanted.ip_mode.value, wanted.address, wanted.mask, wanted.gateway),
            )
        if wanted.socket_host:
            self._compare(
                changes,
                "socket A",
                at.SOCKET_A,
                (current.socket_mode.value, current.socket_host, str(current.socket_port)),
                (wanted.socket_mode.value, wanted.socket_host, str(wanted.socket_port)),
            )
        return tuple(changes)

    def apply(self, changes: Sequence[Change]) -> None:
        """Carry out the changes. Does not save permanently and does not restart — that is what
        ``persist`` and ``restart`` are for, and ``base.configure`` calls all three in order."""
        for change in changes:
            if change.manual:
                continue
            self._transport.request(change.how)

    def persist(self) -> None:
        """``AT+CFGTF`` — without it the settings vanish on the next power cut."""
        self._ask(at.SAVE)

    def restart(self) -> None:
        """``AT+Z``. The device answers and **stops answering right after** — the restart takes
        a few seconds, and the AT session ends along with it."""
        self._transport.request(at.query(at.RESTART))
        self._transport.close()

    def factory_reset(self) -> None:
        """``AT+CLEAR`` — factory settings and a restart. The adapter returns to
        ``192.168.0.7``, so contact after this command usually disappears."""
        self._transport.request(at.query(at.CLEAR))
        self._transport.close()

    # --- helpers ---

    def _ask(self, name: str, *params: object) -> tuple[str, ...]:
        command = at.setting(name, *params) if params else at.query(name)
        return at.parse(self._transport.request(command), name, command=command)

    def _one(self, name: str) -> str:
        fields = self._ask(name)
        return fields[0] if fields else ""

    @staticmethod
    def _compare(
        changes: list[Change],
        what: str,
        name: str,
        current: tuple[str, ...],
        desired: tuple[str, ...],
    ) -> None:
        """One command, one comparison.

        The **whole parameter list** is compared, because the device sets them together:
        changing the bitrate alone overwrites both the identifier and the frame type, so
        the plan has to show the person everything that will go out in that command.
        """
        if current == desired:
            return
        changes.append(
            Change(
                what=what,
                current=",".join(current),
                desired=",".join(desired),
                how=at.setting(name, *desired),
            )
        )


def _fd_data_field(current: UsrSettings, wanted: UsrSettings) -> str:
    """The third parameter of ``AT+CANFD`` — the data-phase bitrate.

    With FD off this value means nothing, so the driver **does not touch it**: it writes
    back what the device already has. Otherwise every plan would carry a change that
    changes nothing, and a plan full of noise stops being read.

    The exception: when the device sits on a computed bitrate that cannot be written back,
    the value from the configuration goes out. The command needs three parameters.
    """
    if not wanted.fd:
        try:
            return at.fd_bitrate_field(current.fd_bitrate)
        except ValueError:
            pass
    return at.fd_bitrate_field(wanted.fd_bitrate)


def _as_usr(desired: CanSettings) -> UsrSettings:
    """The ``CanSettings`` core → full CAN315 settings.

    It exists because the hub has every right to say "500 kbit/s, extended frames" without
    knowing there is a box with a conversion mode on the way. ``LAGUNBUS`` fills in the rest.
    """
    if isinstance(desired, UsrSettings):
        return desired
    return UsrSettings(
        bitrate=desired.bitrate,
        extended=desired.extended,
        fd=desired.fd,
        mode=desired.mode,
        conversion=LAGUNBUS.conversion,
        direction=LAGUNBUS.direction,
        frame_id=LAGUNBUS.frame_id,
        frame_info=LAGUNBUS.frame_info,
    )


def connect(
    host: str,
    *,
    port: int = at.AT_PORT,
    keyword: str = at.AT_KEYWORD,
    timeout: float = 1.0,
    retries: int = 3,
) -> UsrCan315:
    """A driver ready to talk, in one call: ``connect("192.168.0.7")``."""
    driver = UsrCan315(
        NetworkAt(host, port=port, keyword=keyword, timeout=timeout, retries=retries)
    )
    driver.open()
    return driver


def connect_serial(
    path: str, *, baudrate: int = 115_200, timeout: float = 1.0, retries: int = 3
) -> UsrCan315:
    """The same over the cable: ``connect_serial("/dev/tty.usbserial-0001")``.

    The path shows up only once the cable is in — on macOS ``ls /dev/tty.usb*``, on Linux
    ``ls /dev/ttyUSB* /dev/ttyACM*``. No entry means the USB-C socket exposes no serial
    port, and ``connect`` over the network is what is left.
    """
    driver = UsrCan315(SerialAt(path, baudrate=baudrate, timeout=timeout, retries=retries))
    driver.open()
    return driver


def commission(
    settings: UsrSettings,
    *,
    expect: str = "",
    timeout: float = 1.0,
    bind: str = "",
    broadcast: str = "255.255.255.255",
) -> Found:
    """Join **one** adapter: find it, set it up, give it an address. Returns its identity.

    The rule is hard and deliberately so: **one adapter at a time**. When more than one
    device answers the broadcast, this function does not guess — it refuses and prints
    what it saw. The next adapter goes onto the network only once the previous one has an
    address of its own.

    LagunBus nodes have the same problem solved in hardware: as many as like may announce
    themselves at once, and CAN arbitration queues them up with no software involved
    (``core/commissioning.py``). Ethernet has nothing of the sort — two adapters at one
    address answer simultaneously into the same socket. What the hardware does not do, the
    order of operations has to.

    ``expect`` (a MAC) points at one of several — for reconfiguring an adapter on a network
    that is already working, where the others will answer the broadcast and there is
    nothing wrong with that.

    The returned ``Found`` carries the **target address**, not the one the device was found
    at: the identity stays, the route changes. With DHCP the address stays empty — the
    router hands it out at boot, and then it has to be found again by the same MAC.
    """
    if settings.ip_mode is at.IpMode.STATIC and not settings.address:
        raise DriverError(
            "commissioning without an address leaves the adapter on the factory 192.168.0.7, "
            "where the next one will collide with it; set address= or ip_mode=DHCP"
        )

    found = discover(timeout=timeout, bind=bind, broadcast=broadcast)
    if not found:
        raise Unreachable("no adapter answered the broadcast")

    target = _pick(found, expect)
    driver = UsrCan315(NetworkAt(target.address, timeout=timeout))
    driver.open()
    try:
        configure(driver, settings)
    finally:
        driver.close()

    address = settings.address if settings.ip_mode is at.IpMode.STATIC else ""
    return Found(address, target.mac, target.name)


def _pick(found: Sequence[Found], expect: str) -> Found:
    """One device out of those that answered — or a refusal with the list."""
    listing = ", ".join(f"{device.address} ({device.identity})" for device in found)
    if expect:
        wanted = _mac(expect)
        for device in found:
            if _mac(device.mac) == wanted:
                return device
        raise Unreachable(f"no adapter with MAC {expect} answered; answered: {listing}")
    if len(found) > 1:
        raise DriverError(
            f"{len(found)} adapters answered, commission one at a time "
            f"(or name one with expect=<MAC>); answered: {listing}"
        )
    return found[0]


def _mac(value: str) -> str:
    """A MAC for comparison: no separators, upper case."""
    return value.replace(":", "").replace("-", "").upper()
