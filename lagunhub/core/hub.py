"""The CORE thread — the installation model, deadlines, the slow path.

It waits on a queue **with a timeout derived from the nearest deadline**, so a single
thread serves both traffic and the clock
([`ARCHITEKTURA.md`](../ARCHITEKTURA.md) §7.2).

The model belongs to it alone. The fast path sees it only through an immutable
snapshot, which CORE builds and substitutes wholesale — never through a shared
structure.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Sequence

from lagunhub.core.busload import SAMPLE, BusCounts, BusMeter
from lagunhub.core.commissioning import Commissioning
from lagunhub.core.config.apply import apply, bus_label
from lagunhub.core.config.loader import ConfigError
from lagunhub.core.config.model import Config
from lagunhub.core.config.store import ConfigStore
from lagunhub.core.enumeration import Enumerator
from lagunhub.core.events import Event, EventBus, EventKind
from lagunhub.core.model import (
    Installation,
    JoinRecord,
    NodeModel,
    NodeState,
    Snapshot,
    Source,
    Target,
    Uid,
)
from lagunhub.core.readout import Readout, Summary
from lagunhub.core.reconcile import Reconciler
from lagunhub.core.rx import ColdItem, Received, Sent, Task
from lagunhub.core.schedule import Schedule
from lagunhub.core.segments import SegmentReassembler
from lagunhub.core.store import StoreThread
from lagunhub.core.strangers import RETRY, Strangers
from lagunhub.core.traffic import Traffic, TrafficBus
from lagunhub.core.transactions import Outcome, TidAllocator, TransactionManager
from lagunhub.protocol.bus.base import Bus
from lagunhub.protocol.endpoints import device as dev
from lagunhub.protocol.frame import Frame, decode_header
from lagunhub.protocol.registry import TRANSACTIONAL, MessageKind, ResultCode

Publish = Callable[[Snapshot], None]

FROM_NODE = frozenset(
    {
        MessageKind.ALARM,
        MessageKind.EVENT,
        MessageKind.RESPONSE,
        MessageKind.REPORT,
        MessageKind.LONG_RESPONSE,
        MessageKind.QUERY,
    }
)
"""Kinds in which `ADDRESS` is the **sender** (05-mapowanie-can.md §2) — the ones that say
somebody is sitting at that address."""

WATCH = 1.0
"""How often to check whether somebody edited the configuration files by hand.

Polling rather than `inotify` for one reason: development happens on macOS, where
`inotify` does not exist, and one `stat` per second is a cost nobody can see
([`KONFIGURACJA.md`](../../KONFIGURACJA.md) §11.4).
"""


class CoreThread(threading.Thread):
    def __init__(
        self,
        installation: Installation,
        cold: queue.Queue[ColdItem],
        publish: Publish,
        *,
        buses: Sequence[Bus] = (),
        store: StoreThread | None = None,
        config_store: ConfigStore | None = None,
        observer: Callable[[ColdItem], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        tids: TidAllocator | None = None,
        name: str = "core",
    ) -> None:
        super().__init__(name=name, daemon=True)
        self.installation = installation
        self.cold = cold
        self.publish = publish
        self.observer = observer
        self.store = store
        self.config_store = config_store
        """The configuration files. `None` means a hub without configuration — a bare traffic
        watch, say."""

        self.config: Config | None = None
        self.clock = clock
        self.buses = list(buses)
        self.schedule = Schedule()
        self.transactions = TransactionManager(self._send, self.schedule, clock, tids)
        """`tids` is **the binding engine's** allocator in a running hub: a command from a
        binding and one from CORE to the same endpoint must not get the same `Tid` within
        the node's deduplication window (BUG-06). Without it — a counter of its own, which
        is fine only for a hub with no fast path."""
        self.segments = SegmentReassembler(
            self.schedule, clock, self._on_segments, self._on_segments_lost
        )
        self.enumeration = Enumerator(
            installation,
            self._read,
            on_ready=self._on_enumerated,
            on_conflict=self._on_uid_conflict,
            on_failed=self._on_enumeration_failed,
            on_start=self._on_enumeration_started,
        )
        self.commissioning = Commissioning(
            installation, self._send, self.schedule, clock, on_assigned=self._on_assigned
        )
        self.reconcile = Reconciler(installation, self._transact)
        self.readout = Readout(installation, self._transact, on_done=self._on_readout)
        self.strangers = Strangers(
            installation,
            self._read,
            clock,
            on_pending=self._on_pending,
            on_known=self._on_known,
            on_uid_conflict=self._on_uid_conflict,
        )
        self.events = EventBus()
        """What is happening in the installation. Adapters read it; the model stays with
        CORE."""

        self.traffic = TrafficBus()
        """What travels the wire. A second bus, because it answers a different question: an
        event says what happened, traffic says why the state is what it is."""

        self.started = clock()
        """The moment of start-up. The watch shows time **from it**, not from when the
        machine was switched on — `61.204` reads, `409712.969` does not."""

        self.config_reloads = 0
        self.config_rejections = 0
        self.uid_conflicts: list[tuple[int, Uid, int]] = []
        """``(new address, Uid, old address)`` — identity conflicts already reported."""

        self.handled = 0
        self.ignored = 0
        self.published = 0
        self.unmatched_responses = 0
        self.dropped_sends = 0
        self.counts = BusCounts(len(self.buses))
        """What CORE itself sent, per bus. The fast path keeps its own — one writer per
        counter (`core/busload.py`)."""

        self.meter = BusMeter(len(self.buses))
        """Traffic per bus. Starts with CORE's counters only; whoever starts the RX thread
        adds the RX thread's and the binding engine's with `meter.watch`."""
        self.meter.watch(sent=[self.counts])
        self.meter.sample(clock())
        self.schedule.after(clock(), SAMPLE, self._sample)
        self._dirty = True
        self._describe_after: dict[Uid, float] = {}
        """Nodes from the file whose descriptor could not be read, and when their next
        frame may try again — the same minute `Strangers` waits, for the same reason: a
        node that keeps talking and does not answer would start a read on every frame."""
        self._reports: dict[Source, tuple[Target, ...]] = {}
        self._issues_seen = 0
        self._stop = threading.Event()
        if config_store is not None:
            self.schedule.after(clock(), WATCH, self._watch_config)

    def _send(self, bus: int, frame: Frame) -> None:
        """Sending from CORE — transaction retries and later polling."""
        self._traffic(bus, frame, outgoing=True)
        try:
            self.buses[bus].send(frame)
        except IndexError:
            self.dropped_sends += 1  # no such bus — nowhere to count it either
        except OSError:
            # Send queue full (`TransmitQueueFull`) or the bus down (`ENETDOWN`,
            # zadania/zrobione/BUG-04.md): we hand the request to our own timer, because a retry
            # is part of the transaction anyway (ARCHITEKTURA.md §7.1). An uncaught
            # error here used to end the CORE thread — and the API with it, which waits
            # on CORE's queue.
            self.dropped_sends += 1
            self.counts.drop(bus)
        else:
            self.counts.count(bus, frame)

    def _sample(self) -> None:
        """Once a second, off the fast path: the rate is computed from these."""
        self.schedule.after(self.clock(), SAMPLE, self._sample)
        self.meter.sample(self.clock())

    # --- the loop ------------------------------------------------------------

    def run(self) -> None:
        while not self._stop.is_set():
            now = self.clock()
            wait = min(self.schedule.next_deadline(now), 0.2)
            try:
                item = self.cold.get(timeout=wait)
            except queue.Empty:
                item = None
            self.schedule.fire_due(self.clock())
            if item is not None:
                self.handle(item)
            self.flush()

    def pump(self, now: float | None = None) -> int:
        """Handle everything waiting and return. Tests call this instead of start()."""
        now = self.clock() if now is None else now
        handled = 0
        while True:
            try:
                item = self.cold.get_nowait()
            except queue.Empty:
                break
            self.handle(item)
            handled += 1
        self.schedule.fire_due(now)
        self.flush()
        return handled

    def stop(self) -> None:
        self._stop.set()

    # --- snapshot ------------------------------------------------------------

    def touch(self) -> None:
        """Mark that the fast-path image has to be rebuilt."""
        self._dirty = True

    def flush(self) -> None:
        """Rebuild and substitute the snapshot, if the model changed."""
        self._announce_issues()
        if not self._dirty:
            return
        self._dirty = False
        snapshot = self.installation.snapshot()
        self._reports = snapshot.on_report
        self.publish(snapshot)
        self.published += 1
        if self.store is not None:
            # The save goes to a separate thread: fsync on an SD card can go quiet for
            # tens of milliseconds, and CORE has retries to mind in that time.
            self.store.request_save(self.installation)

    # --- watching the file -----------------------------------------------------

    def _watch_config(self) -> None:
        """An edit made in an editor is to be picked up without asking
        (KONFIGURACJA.md §11.4).

        > **Bad configuration has no right to stop a working installation.**

        That is why a failed reload leaves the previous one in force and says what is
        wrong — rather than stopping the hub and zeroing the model.
        """
        self.schedule.after(self.clock(), WATCH, self._watch_config)
        store = self.config_store
        if store is None or not store.changed():
            return
        try:
            config = store.load()
        except (ConfigError, OSError) as exc:
            store.mark_seen()  # otherwise the same issue would be appended every second
            self.installation.issues.append(f"the configuration file was rejected: {exc}")
            self.config_rejections += 1
            return
        self.config_reloads += 1
        self.apply_config(config)

    # --- traffic on the bus ----------------------------------------------------

    def _traffic(self, bus: int, frame: Frame, *, outgoing: bool) -> None:
        """Announce a frame, **expanding names only when somebody is listening**.

        This runs on the slow path for every frame in the installation. At five hundred
        frames per second, turning `0x001 ep4 0x02` into `Output.Toggle` costs more than
        the announcement itself, and most of the time nobody is watching — so the first
        thing this function does is check whether anything is worth doing.
        """
        wire = self.traffic
        if not wire.listeners:
            return
        bus_name = self.buses[bus].name if 0 <= bus < len(self.buses) else str(bus)
        now = self.clock() - self.started
        if frame.is_join:
            wire.publish(
                Traffic(
                    at=now,
                    bus=bus_name,
                    kind="JOIN",
                    outgoing=outgoing,
                    detail=f"ShortId 0x{frame.can_id & 0x00FF_FFFF:08X}",
                    data=frame.data,
                )
            )
            return

        fields = frame.fields
        kind = _kind(fields.kind)
        kind_name = kind
        """The element’s namespace. For a reply it is the kind of the REQUEST, not of the
        reply."""

        tid: int | None = None
        result: str | None = None
        elapsed: float | None = None
        late = False
        retry = 0
        if kind in (MessageKind.RESPONSE, MessageKind.LONG_RESPONSE):
            # A reply's element is named after the REQUEST KIND: `0x02` is either the
            # `Toggle` command or the `Red` attribute, depending on what was asked. A
            # reply the hub cannot pair with a request gets no name at all — the view
            # shows the number rather than a guess (zadania/BUG-03.md).
            kind_name = None
        if kind in TRANSACTIONAL and frame.data:
            code, tid = decode_header(frame.data[0])
            if kind in (MessageKind.RESPONSE, MessageKind.LONG_RESPONSE):
                result = _result_name(code)
                # Also after the transaction closed: the second reply to a retried
                # `Toggle` used to be named from the attribute table — "Red OK".
                asked = self.transactions.asked(frame.can_id, tid)
                if asked is not None:
                    kind_name = _kind(asked.kind)
                    elapsed = self.clock() - asked.started
                    late = asked.late
            elif outgoing:
                # A retry is the same frame with the same `Tid` — without a mark the
                # feed showed two identical rows, and the retry was found only on the
                # board (zadania/BUG-03.md).
                pending = self.transactions.open(frame.can_id, tid)
                if pending is not None and pending.attempts > 1:
                    retry = pending.attempts - 1
                    elapsed = self.clock() - pending.started

        uid = self.installation.by_address.get(fields.address)
        wire.publish(
            Traffic(
                at=now,
                bus=bus_name,
                kind=kind.name if kind is not None else f"0x{fields.kind:X}",
                outgoing=outgoing,
                address=fields.address,
                endpoint=fields.endpoint,
                element=fields.element,
                uid=uid,
                name=self._element_name(uid, fields.endpoint, fields.element, kind_name),
                detail=_detail(frame, kind, tid is not None),
                tid=tid,
                result=result,
                elapsed=elapsed,
                late=late,
                retry=retry,
                data=frame.data,
            )
        )

    def _element_name(
        self, uid: Uid | None, endpoint: int, element: int, kind: MessageKind | None
    ) -> str:
        """`Toggle` instead of `0x02` — as long as the hub knows this endpoint's type."""
        node = self.installation.nodes.get(uid) if uid is not None else None
        model = node.endpoints.get(endpoint) if node else None
        definition = model.definition if model else None
        if definition is None or kind is None:
            return ""
        if kind in (MessageKind.COMMAND, MessageKind.GROUP_COMMAND):
            meta = definition.commands.get(element)
        elif kind in (MessageKind.EVENT, MessageKind.ALARM):
            meta = definition.events.get(element)  # type: ignore[assignment]
        else:
            meta = definition.attributes.get(element)  # type: ignore[assignment]
        return meta.name if meta is not None else ""

    # --- announcing -----------------------------------------------------------

    def _announce_issues(self) -> None:
        """Issues are appended in several places, so we catch them in **one** — by noticing
        the list grew. Otherwise every new writer would have to remember to."""
        issues = self.installation.issues
        while self._issues_seen < len(issues):
            content = issues[self._issues_seen]
            self._issues_seen += 1
            self.events.publish(Event(EventKind.ISSUE, self.clock(), text=content))

    def _announce_state(self, address: int, endpoint: int, element: int, raw: bytes) -> None:
        uid = self.installation.by_address.get(address)
        if uid is None:
            return
        model = self.installation.nodes[uid].endpoints.get(endpoint)
        state = model.attributes.get(element) if model else None
        definition = model.definition if model else None
        meta = definition.attributes.get(element) if definition else None
        self.events.publish(
            Event(
                kind=EventKind.STATE,
                at=self.clock(),
                uid=uid,
                endpoint=endpoint,
                element=element,
                name=meta.name if meta else f"0x{element:02X}",
                value=state.value if state else None,
                raw=raw,
            )
        )

    def _announce_event(self, address: int, endpoint: int, element: int, raw: bytes) -> None:
        uid = self.installation.by_address.get(address)
        if uid is None:
            return
        model = self.installation.nodes[uid].endpoints.get(endpoint)
        definition = model.definition if model else None
        meta = definition.events.get(element) if definition else None
        self.events.publish(
            Event(
                kind=EventKind.EVENT,
                at=self.clock(),
                uid=uid,
                endpoint=endpoint,
                element=element,
                name=meta.name if meta else f"0x{element:02X}",
                raw=raw,
            )
        )

    def _announce_node(self, uid: Uid, state: str) -> None:
        node = self.installation.nodes.get(uid)
        self.events.publish(
            Event(
                kind=EventKind.NODE,
                at=self.clock(),
                uid=uid,
                name=state,
                text=node.name if node else "",
            )
        )

    # --- the slow path --------------------------------------------------------

    def handle(self, item: ColdItem) -> None:
        if self.observer is not None:
            # The watch is a tap, not a consumer. Whatever sits there has no right to
            # stall CORE — pushing to a terminal goes through a queue of its own that
            # drops, not through a blocking write.
            self.observer(item)
        if isinstance(item, Task):
            item.action()
            self.handled += 1
            return
        if isinstance(item, Sent):
            self._on_sent(item)
            return
        self._on_received(item)

    def _on_sent(self, item: Sent) -> None:
        """The fast path sent the command; CORE takes over minding the retries."""
        frame = item.frame
        self._traffic(item.bus, frame, outgoing=True)
        if frame.is_join or not frame.data:
            self.ignored += 1
            return
        if frame.fields.kind not in TRANSACTIONAL:
            self.ignored += 1  # a group command has no Tid and no reply
            return
        _, tid = decode_header(frame.data[0])
        self.transactions.track(item.bus, frame.can_id, frame.data, tid)
        self.handled += 1

    def _on_received(self, item: Received) -> None:
        frame = item.frame
        self._traffic(item.bus, frame, outgoing=False)
        if frame.is_join:
            self.commissioning.on_join(item.bus, frame)
            self.handled += 1
            self.touch()  # assigning an address changes the model
            return
        fields = frame.fields
        now = self.clock()
        if fields.kind in FROM_NODE:
            # Before the kind is dispatched: a report, an event and a reply are all
            # equally proof that somebody sits at this address
            # (06-cykl-zycia-urzadzenia.md §2.3).
            self.strangers.heard(item.bus, fields.address)
            self._first_frame(fields.address)

        if fields.kind == MessageKind.REPORT:
            changed = self.installation.on_report(
                item.bus, fields.address, fields.endpoint, fields.element, frame.data, now
            )
            if changed:
                self._announce_state(fields.address, fields.endpoint, fields.element, frame.data)
            self._fire_report(fields.address, fields.endpoint, fields.element, frame.data, changed)
            self.handled += 1
            return

        if fields.kind in (MessageKind.EVENT, MessageKind.ALARM):
            # The fast path has already carried out the event's effect. CORE notes
            # only that the node spoke — and that is enough not to call it absent.
            self.installation.heard(item.bus, fields.address, now)
            self._announce_event(fields.address, fields.endpoint, fields.element, frame.data)
            if fields.endpoint == 0 and fields.element == dev.BOOTED:
                self._on_booted(item.bus, fields.address, frame.data)
            self.handled += 1
            return

        if fields.kind == MessageKind.RESPONSE:
            self.installation.heard(item.bus, fields.address, now)
            if not self.transactions.on_response(frame):
                self.unmatched_responses += 1
            self.handled += 1
            return

        if fields.kind == MessageKind.LONG_RESPONSE:
            self.installation.heard(item.bus, fields.address, now)
            self.segments.feed(frame)
            self.handled += 1
            return

        self.ignored += 1

    # --- bindings on attribute change -----------------------------------------

    def _fire_report(
        self, address: int, endpoint: int, element: int, value: bytes, changed: bool
    ) -> None:
        """A report is not a fast path — priority `0x8` and no time budget at all.

        A command fires on a change only, or a periodic report would toggle a relay
        every minute. A write fires on every report and carries its value: a display
        field blanks after `ContentTimeout` while the temperature stands still, and the
        periodic report is what keeps it lit (07-hub.md §3.1; zadania/zrobione/FEAT-13.md N8).
        """
        uid = self.installation.by_address.get(address)
        if uid is None:
            return
        for target in self.snapshot_targets(uid, endpoint, element):
            if target.copies_value:
                # Through `_transact`, as an API write: a successful reply lands in the
                # model, so the view shows what the field shows.
                to = Frame(target.can_id).fields
                self._transact(
                    target.bus, to.address, to.endpoint, to.element, MessageKind.WRITE, value
                )
            elif changed:
                self.fire(target)

    def snapshot_targets(self, uid: Uid, endpoint: int, element: int) -> tuple[Target, ...]:
        return self._reports.get((uid, endpoint, element), ())

    def fire(self, target: Target) -> None:
        """Send a binding’s effect. An addressed command and a write are transactions."""
        if target.transactional:
            self.transactions.request(target.bus, target.can_id, target.payload[1:])
        else:
            self._send(target.bus, Frame(target.can_id, target.payload))

    # --- identifying a device -------------------------------------------------

    def _transact(
        self,
        bus: int,
        address: int,
        endpoint: int,
        element: int,
        kind: MessageKind,
        args: bytes,
        on_done: Callable[[Outcome], None] | None = None,
    ) -> None:
        """The hub’s only route onto the bus — and therefore the only place where a
        successful reply reaches the model.

        Also the one place that decides **the form of a write**: a `string` attribute
        always goes as `LONG_WRITE`, however short, and nothing else ever does
        (02-typy-danych.md §2.2). By the attribute's type, not by the value's length —
        so an API call, a reconciliation and a binding write a text the same way.
        """
        from lagunhub.protocol.frame import encode_id

        remembering = self._remembering(bus, address, endpoint, element, kind, args, on_done)
        if kind == MessageKind.WRITE and self._is_string(address, endpoint, element):
            self.transactions.request_long(
                bus,
                encode_id(MessageKind.LONG_WRITE, address, endpoint, element),
                args,
                on_done=remembering,
            )
            return
        self.transactions.request(
            bus, encode_id(kind, address, endpoint, element), args, on_done=remembering
        )

    def _is_string(self, address: int, endpoint: int, element: int) -> bool:
        uid = self.installation.by_address.get(address)
        node = self.installation.nodes.get(uid) if uid is not None else None
        model = node.endpoints.get(endpoint) if node is not None else None
        definition = model.definition if model is not None else None
        meta = definition.attributes.get(element) if definition is not None else None
        return meta is not None and meta.type == "string"

    def _read(
        self,
        bus: int,
        address: int,
        endpoint: int,
        element: int,
        on_done: Callable[[Outcome], None],
    ) -> None:
        self._transact(bus, address, endpoint, element, MessageKind.READ, b"", on_done)

    def _remembering(
        self,
        bus: int,
        address: int,
        endpoint: int,
        element: int,
        kind: MessageKind,
        args: bytes,
        on_done: Callable[[Outcome], None] | None,
    ) -> Callable[[Outcome], None]:
        """A reply to a read, or an acknowledged write, **changes the model**.

        Without this the hub polled a node, compared and forgot: it knew the structure
        of the installation and not a single value the node does not report by itself.
        It only became visible from the view, because until then nobody asked for those
        values.
        """

        def done(outcome: Outcome) -> None:
            if outcome.ok:
                raw = outcome.data if kind == MessageKind.READ else args
                if kind in (MessageKind.READ, MessageKind.WRITE):
                    changed = self.installation.remember(
                        bus, address, endpoint, element, raw, self.clock()
                    )
                    if changed:
                        self._announce_state(address, endpoint, element, raw)
                    self.touch()
            elif outcome.result == ResultCode.UNSUPPORTED_ELEMENT and kind in (
                MessageKind.READ,
                MessageKind.WRITE,
            ):
                self._learn_absent(address, endpoint, element)
            if on_done is not None:
                on_done(outcome)

        return done

    def _learn_absent(self, address: int, endpoint: int, element: int) -> None:
        """An optional attribute the node turned down is remembered **here**, for every
        caller at once.

        It used to be reconciling's own doing, so a read from the view refused the same
        attribute every time and the API kept offering it. One place, the same as for
        values, makes a read, reconciling and reading everything teach the hub alike
        (zadania/zrobione/FEAT-06.md, R9).

        Only an **optional** one: a device may leave out nothing else. A mandatory
        attribute refused is the node failing its type — calling it "set by the device
        itself" would hide that, so the caller reports it instead (R8).
        """
        uid = self.installation.by_address.get(address)
        node = self.installation.nodes.get(uid) if uid is not None else None
        model = node.endpoints.get(endpoint) if node is not None else None
        definition = model.definition if model is not None else None
        meta = definition.attributes.get(element) if definition is not None else None
        if model is not None and meta is not None and meta.optional:
            model.absent.add(element)

    def _on_assigned(self, record: JoinRecord) -> None:
        """After an address is assigned the node will emit `Booted` — and then we identify
        it."""
        self.events.publish(
            Event(
                kind=EventKind.NODE,
                at=self.clock(),
                name=record.state.name,
                text=f"ShortId 0x{record.short_id:08X} → address 0x{record.address:03X}",
            )
        )
        self.touch()

    def _on_pending(self, record: JoinRecord) -> None:
        """A node under an unknown address has been identified and waits for a person."""
        self.events.publish(
            Event(
                kind=EventKind.NODE,
                at=self.clock(),
                name=record.state.name,
                text=(
                    f"ShortId 0x{record.short_id:08X} works at 0x{record.address:03X}, "
                    f"which the hub does not know — admit it or reset it to factory settings"
                ),
            )
        )
        self.touch()

    def _on_known(self, record: JoinRecord) -> None:
        """A node under an unknown address whose `Uid` is in the file: in without asking,
        because the entry is the operator's decision, made before
        (06-cykl-zycia-urzadzenia.md §2.3; lagunbus-spec/DECYZJE.md, "Węzeł z adresem,
        którego `Uid` hub ma w konfiguracji, wchodzi sam")."""
        self.events.publish(
            Event(
                kind=EventKind.NODE,
                at=self.clock(),
                uid=record.uid,
                name=record.state.name,
                text=(
                    f"0x{record.address:03X} is a node from the configuration — "
                    f"enumerating it without asking"
                ),
            )
        )
        self.enumeration.start(record)
        self.touch()

    def _on_enumerated(self, node: NodeModel) -> None:
        self._describe_after.pop(node.uid, None)
        self._announce_node(node.uid, node.state.name)
        self.note_joined(node)
        self.touch()  # a node arrived, so bindings may have a new target
        # The hub writes to the node what it holds itself — that way round, never the
        # other. A blocked one is skipped by the reconciler itself.
        self.reconcile.start(node)

    # --- nodes from the file ----------------------------------------------------

    def _first_frame(self, address: int) -> None:
        """A node taken from the file has spoken: now is the time to read what it is
        (zadania/zrobione/BUG-05.md, N9).

        The frame that came anyway is the occasion, as everywhere after a restart — the
        hub does not poll everybody at start (07-hub.md §5.1). Reconciling follows the
        enumeration by itself, so a change made to the file while the hub was down
        reaches the node too. A blocked node is not read (06-cykl-zycia-urzadzenia.md
        §8).
        """
        uid = self.installation.by_address.get(address)
        node = self.installation.nodes.get(uid) if uid is not None else None
        if node is None or node.described or node.state == NodeState.BLOCKED:
            return
        if self.enumeration.busy(node.short_id) or self.clock() < self._describe_after.get(
            node.uid, 0.0
        ):
            return

        def done(ready: bool) -> None:
            if not ready:
                self._describe_after[node.uid] = self.clock() + RETRY

        self.describe(node, done)

    def describe(self, node: NodeModel, then: Callable[[bool], None] | None = None) -> None:
        """Read the descriptor of a node the hub holds — or wait for the read already
        running. `then` hears whether the model is in place."""
        record = self.installation.records.get(node.short_id)
        if record is None or not record.address:
            record = self.installation.record(node.short_id)
            record.address, record.bus, record.uid = node.address, node.bus, node.uid
            record.state = node.state
        self.enumeration.start(record, then)

    def note_joined(self, node: NodeModel) -> None:
        """Write where the node sits to its entry in the file — `joined`
        (zadania/zrobione/BUG-05.md, D1, N6).

        The file is where an address survives a restart. The hub writes only what it
        learned from the bus, and only when it differs from what the file says, so an
        enumeration of a node whose entry is right touches nothing. A node in the model
        wins over the file: the model came from the bus, and the file may be a backup.

        A file that cannot be written does not stop the node — it works now, and is
        reported, because after the next restart it would not.
        """
        store = self.config_store
        if store is None or not node.address or not node.short_id:
            return
        names = [bus.name for bus in self.buses]
        edited = store.changed()  # by hand, and not picked up yet
        try:
            config = store.set_joined(
                bytes(node.uid),
                node.address,
                bus_label(self.config, names, node.bus),
                node.short_id,
            )
        except (ConfigError, OSError) as exc:
            self.installation.issues.append(
                f"{bytes(node.uid).hex()}: its address could not be written to the "
                f"configuration ({exc}) — after a restart the hub will not know it"
            )
            return
        if config is None:
            return
        if edited:
            # The write took somebody's edit along with it — and marked the file as
            # seen. It has to go in the way every edit does, reconciling included.
            self.apply_config(config)
            return
        self.config = config
        apply(self.installation, config, names)
        self.touch()

    def _on_enumeration_started(self, record: JoinRecord) -> None:
        """Enumeration builds a new model, so a readout of the old one stops
        (zadania/zrobione/FEAT-06.md, R5) — whoever started it, the operator or `Booted` with new
        firmware."""
        if record.uid is not None:
            self.readout.cancel(record.uid)

    def _on_readout(self, node: NodeModel, summary: Summary) -> None:
        """The end of reading everything, as a `NODE` event — the way a failed
        enumeration is told — rather than a new kind clients would have to learn
        (zadania/zrobione/FEAT-06.md, R10)."""
        self.events.publish(
            Event(
                kind=EventKind.NODE,
                at=self.clock(),
                uid=node.uid,
                name="READOUT_DONE",
                text=summary.text(),
            )
        )
        self.touch()

    def _on_enumeration_failed(self, record: JoinRecord, reason: str) -> None:
        """Said out loud, because one case of it is a person waiting: an enumeration they
        asked for (06-cykl-zycia-urzadzenia.md §3.3). Before that it went nowhere — the
        reason was dropped and the record quietly became `ABSENT`."""
        who = record.uid.hex() if record.uid else f"ShortId 0x{record.short_id:08X}"
        self.installation.issues.append(
            f"{who} (0x{record.address:03X}): enumeration failed — {reason}"
        )
        self.events.publish(
            Event(
                kind=EventKind.NODE,
                at=self.clock(),
                uid=record.uid,
                name="ENUMERATION_FAILED",
                text=reason,
            )
        )

    def _on_booted(self, bus: int, address: int, data: bytes) -> None:
        """`Booted` carries the firmware version, so the hub decides **without polling**.

        Re-enumeration happens if and only if the version changed — because only an
        update can add an endpoint or raise a type revision
        (06-cykl-zycia-urzadzenia.md §3.3).
        """
        if len(data) < 6:
            return
        firmware = int.from_bytes(data[2:6], "little")
        self.commissioning.on_booted(bus, address)
        record = self.installation.record_at(bus, address)
        if record is None or record.state in (NodeState.PENDING, NodeState.BLOCKED):
            # An address the hub does not know is `Strangers`' business, and it has
            # already seen this frame (06-cykl-zycia-urzadzenia.md §2.3). A pending node
            # waits for the operator — enumerating it here would let it in by the back
            # door. A blocked one is not read at all (06-cykl-zycia-urzadzenia.md §8).
            return
        if record.uid is None:
            self.enumeration.start(record)
            return
        node = self.installation.nodes.get(record.uid)
        if node is not None and node.firmware != firmware:
            self.enumeration.start(record)

    def _on_uid_conflict(self, address: int, uid: Uid, previous: int) -> None:
        """The same node answers from two addresses — the hub does not settle this.

        It does not guess which address is current, because there is nothing to guess
        from: both are true in the sense that the node answered at each. The cause is
        usually a readdressing whose acknowledgement went missing, or a database
        restored from an incomplete backup.
        """
        if (address, uid, previous) in self.uid_conflicts:
            return
        self.uid_conflicts.append((address, uid, previous))
        self.installation.issues.append(
            f"node {uid.hex()} answers from two addresses "
            f"(0x{previous:03X} and 0x{address:03X}) — the hub cannot settle which is "
            f"current"
        )

    def _on_segments(self, can_id: int, tid: int, value: bytes) -> None:
        if not self.transactions.on_segments(can_id, tid, value):
            self.unmatched_responses += 1

    def _on_segments_lost(self, can_id: int, tid: int, reason: str) -> None:
        del reason  # the log arrives with maintenance, ARCHITEKTURA.md §14.4
        self.transactions.on_segments_lost(can_id, tid)

    # --- requests from the hub ------------------------------------------------

    def request(
        self,
        uid_or_address: int,
        endpoint: int,
        element: int,
        *,
        kind: MessageKind = MessageKind.READ,
        args: bytes = b"",
        on_done: Callable[[Outcome], None] | None = None,
    ) -> None:
        """A read, a write or a command on the hub’s initiative. From the CORE thread only."""
        uid = self.installation.by_address.get(uid_or_address)
        node = self.installation.nodes.get(uid) if uid is not None else None
        bus = node.bus if node is not None else 0
        self._transact(bus, uid_or_address, endpoint, element, kind, args, on_done)

    def transact(
        self,
        bus: int,
        address: int,
        endpoint: int,
        element: int,
        kind: MessageKind,
        args: bytes,
        on_done: Callable[[Outcome], None] | None = None,
    ) -> None:
        """Like `request`, but on a bus the caller names — for a node the model does not
        hold yet, such as a pending one, whose bus only its join record knows."""
        self._transact(bus, address, endpoint, element, kind, args, on_done)

    # --- for adapters and later stages ----------------------------------------

    def apply_config(self, config: Config) -> None:
        """Load the configuration and bring the nodes into agreement with it."""
        self.config = config
        before = self.installation.blocked
        apply(self.installation, config, [bus.name for bus in self.buses])
        self._apply_blocks(before, self.installation.blocked)
        self.touch()
        self.flush()
        for node in list(self.installation.nodes.values()):
            self.reconcile.start(node)

    def _apply_blocks(self, before: frozenset[Uid], now: frozenset[Uid]) -> None:
        """Carry a change of `blocked` in the file over to nodes the hub already knows
        (06-cykl-zycia-urzadzenia.md §8).

        Blocking only changes states and stops reconciling — the bindings follow from the
        snapshot, which leaves out every binding touching a blocked node. Unblocking reads
        the node again rather than trusting the model it had: the node was not served for
        a while, and enumeration is what turns a known address into `READY`. A node with
        no address has nothing to read — it goes the ordinary way when it announces.
        """
        inst = self.installation
        for record in inst.records.values():
            if record.uid in now - before:
                record.state = NodeState.BLOCKED
            elif record.uid in before - now:
                if record.address:
                    record.state = NodeState.JOINED
                    self.enumeration.start(record)
                else:
                    record.state = NodeState.UNKNOWN
        for uid in now - before:
            self.reconcile.cancel(uid)
            self.readout.cancel(uid)
            node = inst.nodes.get(uid)
            if node is not None:
                node.state = NodeState.BLOCKED
                self._announce_node(uid, node.state.name)
        for uid in before - now:
            node = inst.nodes.get(uid)
            if node is not None:
                node.state = NodeState.JOINED

    def forget(self, uid: Uid, *, acknowledged: bool) -> NodeModel | None:
        """The model half of removing a node (06-cykl-zycia-urzadzenia.md §6): forget it,
        and — once it acknowledged its reset — forget having heard its address too, so
        the address can go to the next node, the same one admitted again included."""
        self.reconcile.cancel(uid)
        self.readout.cancel(uid)
        node = self.installation.forget(uid)
        if node is not None and node.address and acknowledged:
            self.strangers.forget(node.address)
        self._announce_node(uid, "REMOVED")
        self.touch()
        return node

    def call(self, action: Callable[[Installation], None]) -> None:
        """A model change **from the CORE thread**. Outside it, `submit` does this."""
        action(self.installation)
        self.touch()

    def submit(self, action: Callable[[], None]) -> None:
        """Run something on the CORE thread. The only route for adapters.

        The model belongs to CORE alone, and reaching into it from another thread is a
        mistake, however briefly it may look like it works.
        """
        try:
            self.cold.put_nowait(Task(action))
        except queue.Full:
            self.dropped_sends += 1


# --- unpacking a frame -----------------------------------------------------------


def _kind(number: int) -> MessageKind | None:
    """A kind, or `None` — because a number this hub does not know yet may appear on the
    bus, and the watch is to show it rather than pass it over."""
    try:
        return MessageKind(number)
    except ValueError:
        return None


def _result_name(code: int) -> str:
    from lagunhub.protocol.registry import ResultCode

    try:
        return ResultCode(code).name
    except ValueError:
        return f"0x{code:02X}"


def _detail(frame: Frame, kind: MessageKind | None, has_header: bool) -> str:
    """Whatever cannot be taken apart into fields. Raw, because we do not know the type
    here."""
    payload = frame.data[1:] if has_header else frame.data
    if not payload:
        return ""
    if kind == MessageKind.REPORT and len(payload) <= 4:
        return str(int.from_bytes(payload, "little"))
    return payload.hex()
