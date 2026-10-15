"""Transactions: `Tid` allocation, retries, deduplication.

A transaction is a request and a reply forming a pair. A read, a write and an addressed
command are transactions; a report and an event are not
([`04`](../lagunbus-spec/04-interakcje.md) §2).

The manager knows neither the bus nor the model — it is given a send function and a
calendar of deadlines. It belongs solely to the CORE thread; the fast path sends by
itself and **hands over the minding** of retries, because the first one falls due only
100 ms later.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Final

from lagunhub.core.schedule import Handle, Schedule
from lagunhub.core.segments import split
from lagunhub.protocol.frame import TID_MASK, Frame, decode_header, encode_header
from lagunhub.protocol.registry import RETRYABLE, ResultCode

TID_COUNT: Final = TID_MASK + 1
"""Eight values. The specification counts on all of them
(04-interakcje.md §2.2)."""

KEY_MASK: Final = 0x01FF_FFFF
"""The address, the endpoint and the element — everything but the kind.

A request and a reply differ **only in kind**: in a command `ADDRESS` is the recipient,
in a reply the same node is the sender. The mask makes one key out of that, with no
table translating one into the other."""


class TidAllocator:
    """A cycling `Tid`, separate for every deduplication key.

    The key is the **request frame identifier**, that is, exactly what the node takes
    into its deduplication buffer: the address, the endpoint and the element number
    ([`04`](../lagunbus-spec/04-interakcje.md) §2.2). That way eight commands to eight
    different endpoints do not clash, even if they go out within five milliseconds.

    **The key is cut down here, not by the caller.** The fast path used to count by the
    whole frame identifier and CORE by the identifier without the kind, so even one
    shared instance held two counters for the same command — both starting at 0, and a
    `Toggle` from the API right after the same `Toggle` from a binding went out with the
    same `Tid` and was swallowed by the node's deduplication (BUG-06).

    **Allocation is shared across all threads and does without a lock.** It rests on
    two atomic CPython operations: ``dict.setdefault`` inserts the counter in one C
    call, and ``next()`` on ``itertools.count`` bumps it in another. This is the same
    class of assumption as substituting the snapshot reference
    ([`ARCHITEKTURA.md`](../ARCHITEKTURA.md) §5.3).
    """

    __slots__ = ("_counters",)

    def __init__(self) -> None:
        self._counters: dict[int, itertools.count[int]] = {}

    def next(self, can_id: int) -> int:
        counter = self._counters.setdefault(can_id & KEY_MASK, itertools.count())
        return next(counter) % TID_COUNT


# --- the transaction manager -----------------------------------------------------

TIMEOUT: Final = 0.100
"""A hundred milliseconds for a reply (04-interakcje.md §2.1)."""

ATTEMPTS: Final = 3
"""Three attempts in all, that is 0, 100 and 200 ms. After that the node is not
answering."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """How the transaction ended."""

    result: ResultCode | None
    """``None`` means silence after three attempts — which differs from an error."""

    data: bytes
    attempts: int
    elapsed: float

    @property
    def answered(self) -> bool:
        return self.result is not None

    @property
    def ok(self) -> bool:
        return self.result == ResultCode.OK


@dataclass(frozen=True, slots=True)
class Asked:
    """What the hub asked — all that is needed to name a reply and time it."""

    kind: int
    """The kind of the **request**. The element namespace depends on it: `0x02` is the
    `Toggle` command or the `Red` attribute (01-model-danych.md §3), and the reply
    itself does not say which."""

    started: float
    """When the first attempt went out. Retries keep `Tid` and this moment alike."""

    late: bool
    """The transaction had already closed when the reply came — an answer to a retry,
    or one after the hub gave up."""


@dataclass
class Pending:
    bus: int
    can_id: int
    payload: bytes
    tid: int
    started: float
    attempts: int = 1
    on_done: Callable[[Outcome], None] | None = None
    timer: Handle | None = None
    segments: tuple[bytes, ...] = ()
    """A `LONG_WRITE`: the payloads of every segment, sent as one request and repeated
    whole (05-mapowanie-can.md §6.6). Empty for an ordinary request, whose `payload` is
    the one frame."""

    awaiting_segments: bool = False
    """The reply came with the `SEGMENTED` code, that is, an announcement rather than a
    result.

    The transaction **carries on** — to the hub this is still one read, only the result
    arrives in several frames (04-interakcje.md §3)."""

    @property
    def key(self) -> tuple[int, int]:
        return self.can_id & KEY_MASK, self.tid


class TransactionManager:
    """Retries and matching replies to requests. Belongs solely to CORE.

    It knows neither the bus nor the model — it is given a send function and a calendar
    of deadlines. That is what makes it checkable without a single socket.
    """

    def __init__(
        self,
        send: Callable[[int, Frame], None],
        schedule: Schedule,
        clock: Callable[[], float],
        tids: TidAllocator | None = None,
    ) -> None:
        self._send = send
        self._schedule = schedule
        self._clock = clock
        self.tids = tids or TidAllocator()
        self._pending: dict[tuple[int, int], Pending] = {}
        self._closed: dict[tuple[int, int], Asked] = {}
        """The last closed request for every key + `Tid` — to name a reply that comes
        after its transaction closed (zadania/BUG-03.md).

        **No time window.** A key + `Tid` is reused only by the hub itself (one counter
        for both threads, `TidAllocator`), and a new request overwrites the entry — so
        until then the entry is the only request a reply could answer. A window would
        lose the name exactly when replies are latest: the third attempt goes out at
        200 ms, and its reply may come well after 300 ms. The size is bounded by the
        allocator: at most eight entries per key the hub has ever sent on."""

        self.opened = 0
        self.answered = 0
        self.retried = 0
        self.lost = 0
        self.segmented = 0
        self.superseded = 0
        """Commands no longer retried because the same binding sent a newer one (`track`)."""

        self._latest: dict[Hashable, Pending] = {}
        """The last command of each binding with arguments from the event. Bounded by the
        bindings: one entry per binding and target."""

    def __len__(self) -> int:
        return len(self._pending)

    # --- requests -------------------------------------------------------------

    def request(
        self,
        bus: int,
        can_id: int,
        args: bytes = b"",
        *,
        on_done: Callable[[Outcome], None] | None = None,
    ) -> Pending:
        """Send a request and mind its retries."""
        tid = self.tids.next(can_id)
        return self.track(
            bus, can_id, bytes([encode_header(tid)]) + args, tid, on_done=on_done, send=True
        )

    def request_long(
        self,
        bus: int,
        can_id: int,
        value: bytes,
        *,
        on_done: Callable[[Outcome], None] | None = None,
    ) -> Pending:
        """A write of a value that never travels in one frame — a `LONG_WRITE`.

        **Still one transaction** (04-interakcje.md §4): one `Tid`, every segment sent at
        once, and the node answers once, after the LAST, with an ordinary `RESPONSE`. Its
        identifier differs from the segments' only in kind, so the key matches as it does
        for every reply. The hundred milliseconds count from the sending; a node that lost
        a segment says nothing (it drops the value), so silence is the cue to repeat — the
        **whole** value, never the missing piece (§6.6). `BUSY`, the node assembling another
        value, is retried like any `BUSY`.
        """
        tid = self.tids.next(can_id)
        segments = tuple(split(value, tid))
        now = self._clock()
        pending = Pending(
            bus=bus,
            can_id=can_id,
            payload=segments[0],
            tid=tid,
            started=now,
            on_done=on_done,
            segments=segments,
        )
        self._pending[pending.key] = pending
        self._closed.pop(pending.key, None)
        self.opened += 1
        self._send_all(pending)
        pending.timer = self._schedule.after(now, TIMEOUT, lambda: self._expire(pending))
        return pending

    def track(
        self,
        bus: int,
        can_id: int,
        payload: bytes,
        tid: int,
        *,
        on_done: Callable[[Outcome], None] | None = None,
        send: bool = False,
        supersedes: Hashable | None = None,
    ) -> Pending:
        """Take over a request **somebody else has already sent**.

        This is how commands from the fast path come in: RX sends them itself, because
        it has the time budget for it, while retries are CORE’s business
        (ARCHITEKTURA.md §4.2).

        `supersedes` — a command of a binding with arguments from the event: the
        binding's earlier command still unanswered is no longer retried (07-hub.md §3.2;
        zadania/devices/DEVICE-02.md B2). A retried `SetLevel(300)` arriving after
        `SetLevel(500)` would move the light back to a value nobody wants any more; the
        newest number is the one that counts, and a lost one is mended by the next turn
        or the report. Only those bindings: two quick `Toggle`s are two presses, and a
        lost first one has to be retried.
        """
        now = self._clock()
        pending = Pending(
            bus=bus, can_id=can_id, payload=payload, tid=tid, started=now, on_done=on_done
        )
        if supersedes is not None:
            older = self._latest.get(supersedes)
            if older is not None and self._pending.get(older.key) is older:
                # Out of the open ones: its timer finds it gone and does nothing, and a
                # late reply to it is "late", as after the hub gave up.
                del self._pending[older.key]
                self.superseded += 1
                self._finish(older, None, b"")
            self._latest[supersedes] = pending
        self._pending[pending.key] = pending
        self._closed.pop(pending.key, None)
        self.opened += 1
        if send:
            self._send(bus, Frame(can_id, payload))
        pending.timer = self._schedule.after(now, TIMEOUT, lambda: self._expire(pending))
        return pending

    # --- replies -------------------------------------------------------------

    def open(self, can_id: int, tid: int) -> Pending | None:
        """The open transaction of a request going out — to tell a retry from the first
        attempt.

        On the wire the two are the same frame with the same `Tid`
        (04-interakcje.md §2.1); only the manager knows how many times it has been sent.
        `_retry` counts the attempt **before** sending, and `track` registers the first
        one before it goes out, so whoever announces the frame sees the right number.
        """
        return self._pending.get((can_id & KEY_MASK, tid))

    def asked(self, can_id: int, tid: int) -> Asked | None:
        """The request this reply answers — open, or the last one closed with this
        key + `Tid`.

        The traffic watch names the element after the request kind and shows the time
        since the request; both are known only to whoever remembers it. Asked
        **before** `on_response`, because that closes the transaction. `None` means the
        hub never sent anything this could answer — the caller must not guess.
        """
        key = (can_id & KEY_MASK, tid)
        pending = self._pending.get(key)
        if pending is not None:
            return Asked(kind=_kind_of(pending.can_id), started=pending.started, late=False)
        return self._closed.get(key)

    def on_response(self, frame: Frame) -> bool:
        """Match a reply to a request. ``False`` means "not mine"."""
        if not frame.data:
            return False
        result, tid = decode_header(frame.data[0])
        pending = self._pending.pop((frame.can_id & KEY_MASK, tid), None)
        if pending is None:
            # A late reply to a request we already abandoned. It is not an error and
            # there is nothing to do with it but note it.
            return False
        if pending.timer is not None:
            pending.timer.cancel()

        code = ResultCode(result) if result in _CODES else None
        if code in RETRYABLE and pending.attempts < ATTEMPTS:
            # BUSY is the only code after which a retry is worth it
            # (04-interakcje.md §9).
            self._retry(pending)
            return True

        if code == ResultCode.SEGMENTED:
            # SEGMENTED is not an error but an announcement: "accepted, the value is
            # too long for one frame, it is coming in segments"
            # (04-interakcje.md §9). We do not end the transaction — the reassembler
            # takes over minding the time.
            pending.awaiting_segments = True
            self._pending[pending.key] = pending
            self.segmented += 1
            return True

        self.answered += 1
        self._finish(pending, code, frame.data[1:])
        return True

    # --- values arriving in segments ------------------------------------------

    def on_segments(self, can_id: int, tid: int, value: bytes) -> bool:
        """The segments assembled into a value — only this is the read’s result."""
        pending = self._pending.pop((can_id & KEY_MASK, tid), None)
        if pending is None:
            return False
        self.answered += 1
        self._finish(pending, ResultCode.OK, value)
        return True

    def on_segments_lost(self, can_id: int, tid: int) -> bool:
        """A value abandoned half way. The **whole request** is repeated
        (05-mapowanie-can.md §6.6)."""
        pending = self._pending.get((can_id & KEY_MASK, tid))
        if pending is None:
            return False
        pending.awaiting_segments = False
        self._expire(pending)
        return True

    # --- retries --------------------------------------------------------------

    def _expire(self, pending: Pending) -> None:
        if self._pending.get(pending.key) is not pending:
            return  # the reply arrived before the deadline fired
        if pending.awaiting_segments:
            return  # waiting for segments; the reassembler minds the time
        if pending.attempts >= ATTEMPTS:
            del self._pending[pending.key]
            self.lost += 1
            self._finish(pending, None, b"")
            return
        self._retry(pending)

    def _retry(self, pending: Pending) -> None:
        """A retry goes out with **the same `Tid`** (04-interakcje.md §2.1).

        This is not a detail: it lets the node recognise the retry in its deduplication
        buffer and send back the remembered reply instead of carrying the command out a
        second time.
        """
        pending.attempts += 1
        self.retried += 1
        self._pending[pending.key] = pending
        now = self._clock()
        self._send_all(pending)
        pending.timer = self._schedule.after(now, TIMEOUT, lambda: self._expire(pending))

    def _send_all(self, pending: Pending) -> None:
        for payload in pending.segments or (pending.payload,):
            self._send(pending.bus, Frame(pending.can_id, payload))

    def _finish(self, pending: Pending, code: ResultCode | None, data: bytes) -> None:
        self._closed[pending.key] = Asked(
            kind=_kind_of(pending.can_id), started=pending.started, late=True
        )
        if pending.on_done is None:
            return
        pending.on_done(
            Outcome(
                result=code,
                data=data,
                attempts=pending.attempts,
                elapsed=self._clock() - pending.started,
            )
        )


_CODES: Final = {code.value for code in ResultCode}


def _kind_of(can_id: int) -> int:
    return (can_id >> 25) & 0xF
