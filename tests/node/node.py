"""The core of the node double.

The overwhelming majority of a node's behaviour does not depend on what that node does:
a key and a relay differ by one functional endpoint, and all the rest — identity,
diagnostics, transactions, reporting, event counters — is shared. So it lives here and is
written once.

The node has no thread of its own. ``poll()`` and ``tick()`` are called from outside, so
a test drives the time and waits for nothing.
"""

from __future__ import annotations

import heapq
import itertools
import random
import time
from collections.abc import Callable
from typing import Final, TypeVar

from lagunhub.core.segments import split
from lagunhub.protocol.bus.base import Bus, TransmitQueueFull
from lagunhub.protocol.endpoints import device as dev
from lagunhub.protocol.frame import (
    ADDRESS_UNASSIGNED,
    Frame,
    decode_header,
    decode_join_id,
    encode_header,
    encode_id,
    encode_join_id,
)
from lagunhub.protocol.registry import MessageKind, NodeClass, ResultCode
from lagunhub.protocol.values import encode
from tests.node.commissioning import ASSIGN_WINDOW, Joining
from tests.node.dedup import DedupBuffer
from tests.node.endpoints.base import Endpoint
from tests.node.endpoints.device import Device, short_id
from tests.node.endpoints.diagnostics import Diagnostics
from tests.node.endpoints.groups import Groups
from tests.node.endpoints.output import Output
from tests.node.endpoints.scenes import Scenes
from tests.node.faults import Faults

E = TypeVar("E", bound=Endpoint)

EP_DEVICE: Final = 0
EP_DIAGNOSTICS: Final = 1
EP_GROUPS: Final = 2
EP_SCENES: Final = 3
FIRST_FUNCTIONAL: Final = 4

SEGMENT_TIMEOUT: Final = 0.100
"""Silence after which a value under assembly is dropped (05-mapowanie-can.md §6.6)."""

TX_CAPACITY: Final = 16
"""The controller's transmit queue. On overflow the oldest REPORT is lost, never an event
or a reply (types/0x0002-diagnostics.md §5.3)."""


class NodeCore:
    """One node double on one bus."""

    def __init__(
        self,
        *,
        uid: bytes,
        device_type: int,
        node_class: NodeClass = NodeClass.UNSPECIFIED,
        functional: dict[int, Endpoint] | None = None,
        vendor_id: int = 0,
        firmware: int = 0x0001_0000,
        address: int = ADDRESS_UNASSIGNED,
        clock: Callable[[], float] = time.monotonic,
        name: str = "node",
    ) -> None:
        if not 8 <= len(uid) <= 16:
            raise ValueError("a Uid is 8–16 bytes (types/0x0001-device.md §2.1)")
        self.uid = uid
        self.node_class = node_class
        self.name = name
        self.clock = clock
        self.bus: Bus | None = None

        self._ready = False
        self._started_at = clock()
        self._event_seq: dict[int, int] = {}
        self._dedup = DedupBuffer()
        self._last_report: dict[int, float] = {}
        self._reboot_at: float | None = None
        self._pending_address: int | None = None
        self._factory_reset_pending = False
        self.faults = Faults()
        self._segments_pending = 0
        self._long: _Assembly | None = None
        """The one LONG_WRITE being assembled (05-mapowanie-can.md §6.7)."""
        self.joining: Joining | None = None
        """Not empty means the state WITHOUT AN ADDRESS (06-cykl-zycia-urzadzenia.md §1.1)."""
        self._tx: list[tuple[int, int, Frame]] = []
        self._tx_seq = itertools.count()
        self._batching = 0
        self.sent: int = 0
        self.dropped: int = 0

        functional = dict(functional or {})
        for number in functional:
            if number < FIRST_FUNCTIONAL:
                raise ValueError(
                    "functional endpoints are numbered from 4 (06-cykl-zycia-urzadzenia.md §3.2)"
                )
        expected = list(range(FIRST_FUNCTIONAL, FIRST_FUNCTIONAL + len(functional)))
        if sorted(functional) != expected:
            raise ValueError(f"functional endpoints have to be contiguous: {expected}")

        device = Device()
        device.uid = uid
        device.values.update(
            {
                dev.SHORT_ID: short_id(uid),
                dev.DEVICE_TYPE: device_type,
                dev.VENDOR_ID: vendor_id,
                dev.FIRMWARE_VERSION: firmware,
                dev.ENDPOINT_COUNT: len(functional),
                dev.ADDRESS: address,
            }
        )

        self.endpoints: dict[int, Endpoint] = {
            EP_DEVICE: device,
            EP_DIAGNOSTICS: Diagnostics(),
            EP_GROUPS: Groups(),
        }
        # ep3 exists exactly when there is at least one Output among the functional
        # endpoints (01-model-danych.md §1.2). The rule can be worked out, so we work it
        # out here.
        if any(isinstance(e, Output) for e in functional.values()):
            self.endpoints[EP_SCENES] = Scenes()
        self.endpoints.update(functional)

        for number, endpoint in self.endpoints.items():
            endpoint.number = number
            endpoint.node = self

    # --- identity -----------------------------------------------------------

    @property
    def device(self) -> Device:
        return self.endpoint(EP_DEVICE, Device)

    def endpoint(self, number: int, kind: type[E]) -> E:
        """An endpoint of a known type — so that a press() call can be checked
        statically."""
        endpoint = self.endpoints[number]
        if not isinstance(endpoint, kind):
            raise TypeError(f"ep{number} is not of type {kind.__name__}")
        return endpoint

    @property
    def address(self) -> int:
        return self.device.num(dev.ADDRESS)

    @property
    def uptime(self) -> int:
        return int(self.clock() - self._started_at)

    @property
    def ready(self) -> bool:
        return self._ready

    # --- start, 0x0001-device §5.1 ------------------------------------------

    def start(self, bus: Bus, *, reset_reason: int = dev.ResetReason.POWER_ON) -> None:
        """The order is settled and must not be shortened.

        A node that answered before it had come to itself would give values from before
        that — and the hub would have no way of telling them from real ones.
        """
        for endpoint in self.endpoints.values():  # 1. the endpoints
            endpoint.tick(self.clock())
        self.bus = bus  # 2. attaching to the bus
        self._started_at = self.clock()
        self.device.values[dev.LAST_RESET_REASON] = reset_reason
        self.device.bump_boot_count()
        if not self.address:
            # A node without an address does not emit Booted — it has to get one first
            # (§5.3).
            self.joining = Joining(rng=random.Random(self.device.num(dev.SHORT_ID)))
            self.joining.schedule_next(self.clock())
            self._ready = True
            return
        self._ready = True  # 3. only now Booted
        self.send_event(
            EP_DEVICE,
            dev.BOOTED,
            encode("enum8", reset_reason) + encode("u32", self.device.values[dev.FIRMWARE_VERSION]),
        )

    def stop(self) -> None:
        self._ready = False
        self.bus = None

    # --- the loop -----------------------------------------------------------

    def poll(self) -> None:
        if self.bus is None:
            return
        while (frame := self.bus.recv()) is not None:
            self._batching += 1
            try:
                self._handle(frame)
            finally:
                self._batching -= 1
            self.flush()

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        if self.joining is not None:
            if self.joining.due(now):
                self._announce(now)
            self.flush()
            return
        if self._factory_reset_pending:
            # Erase, then start over: without an address the node goes back to joining
            # (06-cykl-zycia-urzadzenia.md §7).
            self._factory_reset_pending = False
            self.factory_reset()
            bus = self.bus
            self.stop()
            if bus is not None:
                self.start(bus, reset_reason=dev.ResetReason.SOFTWARE)
            return
        if self._reboot_at is not None and now >= self._reboot_at:
            self._reboot_at = None
            bus = self.bus
            self.stop()
            if bus is not None:
                self.start(bus, reset_reason=dev.ResetReason.SOFTWARE)
            return
        for endpoint in self.endpoints.values():
            endpoint.tick(now)
        self._periodic_reports(now)
        self.flush()

    # --- joining, 03-adresacja.md §5 ----------------------------------------

    def _announce(self, now: float) -> None:
        """The frame's identifier is derived from the `ShortId`.

        That is not a cosmetic detail but the condition on which the mechanism works:
        arbitration is collision-free only when the identifiers differ. If every
        unassigned node sent one shared identifier, they would collide destructively and
        block the bus for one another (05-mapowanie-can.md §4.2).
        """
        assert self.joining is not None
        short_id = self.device.num(dev.SHORT_ID)
        self._send(
            Frame(
                encode_join_id(announce=True, short_id=short_id),
                short_id.to_bytes(4, "little") + bytes([int(self.node_class)]) + b"\x00" * 3,
            )
        )
        self.joining.schedule_next(now + ASSIGN_WINDOW)

    def _on_assignment(self, frame: Frame) -> None:
        if self.joining is None or len(frame.data) < 6:
            return
        short_id = self.device.num(dev.SHORT_ID)
        if int.from_bytes(frame.data[:4], "little") != short_id:
            return  # an assignment for somebody else — we filter by the full ShortId
        address = int.from_bytes(frame.data[4:6], "little")
        if not address:
            return

        self.device.values[dev.ADDRESS] = address
        self.joining = None
        bus = self.bus
        self.stop()
        if bus is not None:
            self.start(bus, reset_reason=dev.ResetReason.POWER_ON)

    # --- receiving ----------------------------------------------------------

    def _handle(self, frame: Frame) -> None:
        if frame.is_join:
            if not decode_join_id(frame.can_id).announce:
                self._on_assignment(frame)
            return
        if not self._ready or self.joining is not None:
            return
        fields = frame.fields
        kind = fields.kind
        if kind not in (
            MessageKind.READ,
            MessageKind.WRITE,
            MessageKind.COMMAND,
            MessageKind.LONG_WRITE,
        ):
            return  # other nodes' reports and events are for the hub alone
        if fields.address != self.address:
            return
        if not frame.data:
            return
        if kind == MessageKind.LONG_WRITE:
            self._take_segment(fields.endpoint, fields.element, frame.data)
            return

        result_code, tid = decode_header(frame.data[0])
        del result_code  # in a request the result field is reserve
        payload = frame.data[1:]
        endpoint = self.endpoints.get(fields.endpoint)

        if endpoint is None:
            self._respond(fields.endpoint, fields.element, tid, ResultCode.UNSUPPORTED_ENDPOINT)
            return

        if kind == MessageKind.READ:
            result, value = endpoint.read(fields.element)
            if result == ResultCode.SEGMENTED:
                self._read_long(endpoint, fields.endpoint, fields.element, tid)
                return
            self._respond(fields.endpoint, fields.element, tid, result, value)
        elif kind == MessageKind.WRITE:
            result = endpoint.write(fields.element, payload)
            self._respond(fields.endpoint, fields.element, tid, result)
        else:
            self._command(endpoint, fields.endpoint, fields.element, tid, payload)

    def _read_long(self, endpoint: Endpoint, number: int, element: int, tid: int) -> None:
        """The reply announces the segments, and they travel right after it
        (05-mapowanie-can.md §6)."""
        value = endpoint.long_value(element)
        if value is None:
            self._respond(number, element, tid, ResultCode.UNSUPPORTED_ELEMENT)
            return
        if self._segments_pending:
            # A node assembles at most one value in each direction at a time
            # (05-mapowanie-can.md §6.7).
            self._respond(number, element, tid, ResultCode.BUSY)
            return

        self._respond(number, element, tid, ResultCode.SEGMENTED)
        can_id = encode_id(MessageKind.LONG_RESPONSE, self.address, number, element)
        for index, payload in enumerate(split(value, tid)):
            if self.faults.take_drop_segment(index):
                continue
            self._send(Frame(can_id, payload))
            self._segments_pending += 1

    def _take_segment(self, number: int, element: int, data: bytes) -> None:
        """One segment of a LONG_WRITE — the same rules as the C node
        (lagunbus-node `node.c`, `take_segment`): segment 0 opens or restarts, segment 0
        of another value while one is open gets BUSY, a gap or 100 ms of silence drops
        the value without a word, the LAST is answered once with the Tid of segment 0."""
        if len(data) < 2:
            return
        now = self.clock()
        if self._long is not None and now - self._long.last >= SEGMENT_TIMEOUT:
            self._long = None
        tid, index, last = data[0] & 0x07, data[1], bool(data[0] & 0x80)
        open_ = self._long
        same = open_ is not None and (open_.endpoint, open_.element) == (number, element)
        if index == 0:
            if open_ is not None and not same:
                self._respond(number, element, tid, ResultCode.BUSY)
                return
            open_ = self._long = _Assembly(number, element, tid)
        elif open_ is None or not same:
            return
        elif index != open_.next:
            self._long = None
            return
        open_.data += data[2:]
        open_.next += 1
        open_.last = now
        if not last:
            return
        self._long = None
        endpoint = self.endpoints.get(number)
        result = (
            ResultCode.UNSUPPORTED_ENDPOINT
            if endpoint is None
            else endpoint.write_long(element, bytes(open_.data))
        )
        self._respond(number, element, open_.tid, result)

    def _command(
        self, endpoint: Endpoint, number: int, command: int, tid: int, args: bytes
    ) -> None:
        now = self.clock()
        key = (tid, number, command)
        remembered = self._dedup.lookup(key, now)
        if remembered is not None:
            result, data = remembered
        elif (busy := self.faults.take_busy()) is not None:
            result, data = busy, b""
        else:
            result, data = endpoint.command(command, args)
            # What is remembered is **the result of commands carried out**
            # (04-interakcje.md §2.2). A refusal changed nothing, so repeating it is
            # harmless — while a remembered one would deadlock retrying after BUSY
            # for ever.
            if result == ResultCode.OK:
                self._dedup.remember(key, result, data, now)
        self._respond(number, command, tid, result, data)

    def _respond(
        self,
        endpoint: int,
        element: int,
        tid: int,
        result: ResultCode,
        value: bytes = b"",
    ) -> None:
        if self.faults.take_drop():
            return  # request carried out, reply lost — the hardest case
        payload = bytes([encode_header(tid, result)]) + value
        self._send(Frame(encode_id(MessageKind.RESPONSE, self.address, endpoint, element), payload))

    # --- sending ------------------------------------------------------------

    def send_event(self, endpoint: int, event: int, params: bytes = b"") -> None:
        """An event carries a sequence counter — a separate one for each endpoint
        (01-model-danych.md §3.3)."""
        seq = self._event_seq.get(endpoint, 0)
        self._event_seq[endpoint] = (seq + 1) & 0xFF
        self._send(
            Frame(
                encode_id(MessageKind.EVENT, self.address, endpoint, event),
                bytes([seq]) + params,
            )
        )

    def send_report(self, endpoint: int, attribute: int) -> None:
        result, value = self.endpoints[endpoint].read(attribute)
        if result != ResultCode.OK:
            return
        self._send(Frame(encode_id(MessageKind.REPORT, self.address, endpoint, attribute), value))
        self._last_report[endpoint] = self.clock()

    def _send(self, frame: Frame) -> None:
        """A frame goes into the transmit queue, not straight onto the bus.

        A real CAN controller lets out of the queue the one with the lowest identifier,
        so a reply (0x7) overtakes a report (0x8) even though it came about later.
        A double that sent in call order would teach the hub an order it will never see
        on the bus.
        """
        heapq.heappush(self._tx, (frame.can_id, next(self._tx_seq), frame))
        if len(self._tx) > TX_CAPACITY:
            self._drop_oldest_report()
        if not self._batching:
            self.flush()

    def _drop_oldest_report(self) -> None:
        reports = [
            (seq, index)
            for index, (_, seq, frame) in enumerate(self._tx)
            if not frame.is_join and frame.fields.kind == MessageKind.REPORT
        ]
        if not reports:
            return  # nothing but events and replies — none of them may be dropped
        _, index = min(reports)
        del self._tx[index]
        heapq.heapify(self._tx)
        self.dropped += 1

    def flush(self) -> None:
        """Let the transmit queue out in arbitration order."""
        if self.bus is None or self.faults.silent(self.clock()):
            return
        while self._tx:
            entry = heapq.heappop(self._tx)
            frame = entry[2]
            try:
                self.bus.send(frame)
                self.sent += 1
                if not frame.is_join and frame.fields.kind == MessageKind.LONG_RESPONSE:
                    self._segments_pending -= 1
            except TransmitQueueFull:
                heapq.heappush(self._tx, entry)
                return

    def _periodic_reports(self, now: float) -> None:
        from lagunhub.protocol.registry import CommonAttribute

        for number, endpoint in self.endpoints.items():
            if CommonAttribute.ReportInterval not in endpoint.spec:
                continue
            interval = endpoint.num(CommonAttribute.ReportInterval)
            if not interval:
                continue
            if now - self._last_report.get(number, self._started_at) < float(interval):
                continue
            self._last_report[number] = now
            for attribute, meta in endpoint.spec.items():
                if meta.reported:
                    self.send_report(number, attribute)

    # --- driving it from outside --------------------------------------------

    def request_reboot(self, delay: float) -> None:
        self._reboot_at = self.clock() + delay

    def request_factory_reset(self) -> None:
        self._factory_reset_pending = True

    def request_address_change(self, address: int) -> None:
        """A deferred address change — **and the deferring is where it ends**.

        Nothing reads this field, so the double acknowledges a write to `Address` and does
        not change its address. The hub does not use this route: it assigns the address at
        joining. Written down here so that it does not look as if it worked.
        """
        self._pending_address = address

    def factory_reset(self) -> None:
        """Erases what a person wrote and returns to the state without an address.

        A real node clears its persistent memory while doing this. The double has none —
        it lives as long as the test — so zeroing the values and the address is enough.
        """
        for endpoint in self.endpoints.values():
            for attribute, meta in endpoint.spec.items():
                if meta.persist and meta.writable:
                    endpoint.values[attribute] = meta.default
        self.device.values[dev.ADDRESS] = ADDRESS_UNASSIGNED


class _Assembly:
    """A LONG_WRITE under assembly — keyed by endpoint and element, bound to the Tid of
    its segment 0."""

    __slots__ = ("data", "element", "endpoint", "last", "next", "tid")

    def __init__(self, endpoint: int, element: int, tid: int) -> None:
        self.endpoint = endpoint
        self.element = element
        self.tid = tid
        self.next = 0
        self.last = 0.0
        self.data = bytearray()
