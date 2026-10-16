"""The hub API — a named list of operations, shared by every adapter.

Why this exists at all, given that `CoreThread` already has public methods: there will
be three adapters (HTTP, CLI, MQTT) and each of them, reaching into the core directly,
would invent a list of operations **of its own**. After a year there would be three
different definitions of what "toggle the relay" means, and none of them would be the
right one.

Three principles it stands on:

1. **Operations return data, not strings.** A string can make one view; data can make
   all of them. The full-screen view is not to parse the CLI’s output
   ([`ARCHITEKTURA.md`](../ARCHITEKTURA.md) §10.3).
2. **The data is ready to serialise.** A `Uid` leaves here as a hex string, `bytes` as
   hex, `ABSENT` as `None`. The HTTP adapter has nothing to decide, and the CLI and
   MQTT get exactly the same thing.
3. **An adapter does not touch the model.** The model belongs to the CORE thread and
   everything — reads included — goes through its queue.

The third principle differs from what [`ARCHITEKTURA.md`](../ARCHITEKTURA.md) §10.7
describes: there a read reached for a second immutable view (`read_model`), published
by CORE on every change. We dropped it because **the cost ran the wrong way**: the view
would have to be rebuilt on every report from every node, while a person reads it —
that is, rarely. Here it is the other way round: it costs nothing until somebody asks,
and when they do, the view is built on the thread that owns the model. A whole class of
bugs disappears along the way: there is no second view that could drift from the first.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import fields, replace
from typing import Any, Protocol

from lagunhub import __version__
from lagunhub import buses as bus_setup
from lagunhub.api.errors import ApiError, Problem
from lagunhub.api.views import (
    ActionView,
    ArgView,
    AttributeView,
    BindingView,
    BusSetupView,
    BusView,
    ChangeView,
    CommandView,
    CommissioningView,
    ConfigView,
    DiffRow,
    EndpointView,
    EnumerateView,
    EventTypeView,
    EventView,
    HealthView,
    HubInfo,
    JoinView,
    NodeSummary,
    NodeView,
    Plain,
    ReadoutView,
    ReconcileView,
    RemoveView,
    TargetView,
    TrafficView,
)
from lagunhub.buswatch import BusWatch
from lagunhub.core.bindings import BindingEngine
from lagunhub.core.config.loader import ConfigError
from lagunhub.core.config.model import BusConfig, Config
from lagunhub.core.config.store import ConfigStore, ConflictError
from lagunhub.core.events import DEPTH, Event, Subscription
from lagunhub.core.hub import CoreThread
from lagunhub.core.model import (
    AttributeState,
    EndpointModel,
    EndpointWanted,
    Installation,
    JoinRecord,
    NodeModel,
    NodeState,
    Uid,
    Wanted,
)
from lagunhub.core.traffic import Traffic
from lagunhub.core.transactions import Outcome
from lagunhub.drivers.base import AdapterDriver, Change, DriverError, Rejected
from lagunhub.drivers.socketcan import SocketCanDriver
from lagunhub.protocol.endpoints import device as dev
from lagunhub.protocol.endpoints import sense as sns
from lagunhub.protocol.endpoints.definition import (
    AttributeDef,
    Field,
    TypeDef,
    encode_fields,
    field_type,
)
from lagunhub.protocol.endpoints.enums import choices, coerce, render
from lagunhub.protocol.quantities import Quantity
from lagunhub.protocol.registry import MessageKind, ResultCode
from lagunhub.protocol.values import (
    ABSENT,
    SCALARS,
    STRING_PREFIX,
    Value,
    encode,
    encode_string,
    in_range,
)

DEFAULT_TIMEOUT = 2.0
"""A transaction gives up after three attempts, that is, after ~300 ms. Two seconds is
slack for the CORE queue, not for the bus."""


# --- streams ---------------------------------------------------------------------


class Stream[T, V]:
    """A stream for one listener, translated into views on the way.

    It has two ways of being read, because there are two different needs: `for` for
    somebody who simply wants everything, and `get(timeout)` for somebody who has to
    **do something during the silence** — send a heartbeat, say, so that a broken
    connection does not look like a quiet installation.
    """

    __slots__ = ("_convert", "_sub")

    def __init__(self, sub: Subscription[T], convert: Callable[[T], V]) -> None:
        self._sub = sub
        self._convert = convert

    @property
    def dropped(self) -> int:
        """How much this listener lost by not keeping up. It loses at its own end, not at the
        hub."""
        return self._sub.dropped

    @property
    def closed(self) -> bool:
        return self._sub.closed

    def get(self, timeout: float) -> V | None:
        """`None` means **silence**, not an ending — an ending says `closed`."""
        item = self._sub.get(timeout)
        return self._convert(item) if item is not None else None

    def __iter__(self) -> Iterator[V]:
        return (self._convert(item) for item in self._sub)


EventStream = Stream[Event, "EventView"]
TrafficStream = Stream[Traffic, "TrafficView"]


# --- waiting ---------------------------------------------------------------------


class Waiter(Protocol):
    """How to wait for an answer from CORE.

    By default it simply blocks the adapter’s thread — and that is fine, because it
    stalls one request rather than the hub
    ([`ARCHITEKTURA.md`](../ARCHITEKTURA.md) §10.7).

    Tests substitute a function of their own here, because in them CORE is not a
    separate thread but a loop driven by the test’s clock. That way `HubApi` need not
    know whether the hub runs by itself or somebody is winding it forward.
    """

    def __call__(self, future: Future[Any], timeout: float) -> Any: ...


def block(future: Future[Any], timeout: float) -> Any:
    return future.result(timeout=timeout)


DriverFactory = Callable[[BusConfig], AdapterDriver]
"""The driver for the hardware under a bus. Injected so tests need no Linux."""


def socketcan_driver(bus: BusConfig) -> AdapterDriver:
    return SocketCanDriver(bus.channel)


# --- API -------------------------------------------------------------------------


class HubApi:
    """The hub’s operations. The only thing an adapter sees."""

    def __init__(
        self,
        core: CoreThread,
        *,
        engine: BindingEngine | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        wait: Waiter = block,
        drivers: DriverFactory = socketcan_driver,
        watch: BusWatch | None = None,
    ) -> None:
        self.core = core
        self.drivers = drivers
        self.watch = watch
        """The watch over SocketCAN buses: told about a planned break, asked for the
        state of a link (zadania/zrobione/BUG-04.md). `None` without SocketCAN buses."""
        self._applying = threading.Lock()
        """One `apply` at a time: two would interleave their `ip link set … down/up`."""
        self.engine = engine
        """`BindingEngine` — purely so that `health()` has a histogram."""

        self.timeout = timeout
        self.wait = wait

    # --- reading -----------------------------------------------------------------

    def info(self) -> HubInfo:
        """What one is talking to. Cheap, because a client calls it on every connection."""
        return self._ask(self._info)

    def nodes(self) -> tuple[NodeSummary, ...]:
        """Everything the hub knows about the installation — configured but absent nodes
        included."""
        return self._ask(self._nodes)

    def node(self, ref: str) -> NodeView:
        return self._ask(lambda inst: self._node(inst, self._find(inst, ref)))

    def endpoint(self, ref: str, number: int) -> EndpointView:
        def build(inst: Installation) -> EndpointView:
            uid = self._find(inst, ref)
            node = inst.nodes.get(uid)
            if node is None or number not in node.endpoints:
                raise ApiError(Problem.NOT_FOUND, f"{ref}: there is no endpoint {number}")
            return self._endpoint(inst, uid, node.endpoints[number])

        return self._ask(build)

    def bindings(self) -> tuple[BindingView, ...]:
        return self._ask(self._bindings)

    def issues(self) -> tuple[str, ...]:
        return self._ask(lambda inst: tuple(inst.issues))

    def health(self) -> HealthView:
        return self._ask(self._health)

    def commissioning(self) -> CommissioningView:
        return self._ask(self._commissioning)

    # --- buses (zadania/FEAT-01.md) ------------------------------------------------
    #
    # The driver runs on the CALLER's thread, not on CORE: `ip` is a subprocess and may
    # take seconds, CORE answers nodes in milliseconds. From CORE comes only what is
    # CORE's — the file's entries and which buses are open with how many nodes on them.

    def buses(self) -> tuple[BusSetupView, ...]:
        """Every bus in `hub.yaml` with the state of its hardware. Changes nothing."""
        entries, open_channels, counts = self._ask(self._bus_entries)
        return tuple(self._setup(bus, open_channels, counts) for bus in entries)

    def bus(self, kind: str, name: str) -> BusSetupView:
        """One bus, under its own kind — each kind has its own contract, and its own
        paths in the API (`/buses/socketcan/{name}`)."""
        entries, open_channels, counts = self._ask(self._bus_entries)
        return self._setup(self._bus_entry(entries, kind, name), open_channels, counts)

    def bus_apply(self, kind: str, name: str) -> BusSetupView:
        """Carry out what the hub may do itself; what only a person can stays listed.

        This takes the interface down and up — a few hundred milliseconds without a bus
        — which is why it is asked for and never done by looking (makiety, A3). A socket
        the hub has on it stays bound through the bounce. The watch is told first: a bus
        the hub takes down itself is a planned break, not a failure (zadania/zrobione/BUG-04.md).
        """
        entries, _, _ = self._ask(self._bus_entries)
        bus = self._bus_entry(entries, kind, name)
        with self._applying, self._planned(bus.channel):
            driver = self.drivers(bus)
            try:
                driver.open()
                try:
                    plan = driver.plan(bus_setup.settings(bus))
                    automatic = [change for change in plan if not change.manual]
                    if automatic:
                        driver.apply(automatic)
                finally:
                    driver.close()
            except Rejected as exc:
                raise ApiError(
                    Problem.HARDWARE, f"{bus.name}: {exc}{bus_setup.sudo_hint(exc)}"
                ) from None
            except (DriverError, ValueError) as exc:
                raise ApiError(Problem.HARDWARE, f"{bus.name}: {exc}") from None
        return self.bus(kind, name)

    # --- control -----------------------------------------------------------------

    def accept_new(self, seconds: float = 900.0) -> CommissioningView:
        """Open the joining window. It expires on its own — there is no "forever" mode."""

        def open_bus(inst: Installation) -> CommissioningView:
            self.core.commissioning.accept_new(seconds)
            return self._commissioning(inst)

        return self._ask(open_bus)

    def admit(self, short_id: int) -> CommissioningView:
        """Let in a node found under an address the hub did not know
        (06-cykl-zycia-urzadzenia.md §2.3). It keeps that address and goes through
        enumeration like any newly joined node."""

        def admit_it(inst: Installation) -> CommissioningView:
            record = self._stranger(inst, short_id)
            record.state = NodeState.JOINED
            self.core.enumeration.start(record)
            self.core.touch()
            return self._commissioning(inst)

        return self._ask(admit_it)

    def factory_reset(self, short_id: int) -> ActionView:
        """Send a node found under an unknown address back to joining
        (06-cykl-zycia-urzadzenia.md §2.3).

        The node forgets its address and announces itself like a new one, so it lands
        with the accepting mode like any unknown device. The hub gives up its record of
        the address only once the node **acknowledged** the reset — a node that did not
        answer still sits there.
        """
        future: Future[Outcome] = Future()

        def task() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                record = self._stranger(self.core.installation, short_id)
            except BaseException as exc:
                future.set_exception(exc)
                return
            address = record.address

            def done(outcome: Outcome) -> None:
                if outcome.ok and record.address == address:
                    self.core.installation.addresses.release(address)
                    self.core.strangers.forget(address)
                    record.address = 0
                    self.core.touch()
                future.set_result(outcome)

            self.core.transact(
                record.bus,
                address,
                0,
                dev.FACTORY_RESET,
                MessageKind.COMMAND,
                encode("u32", dev.FACTORY_RESET_MAGIC),
                done,
            )

        self.core.submit(task)
        return _outcome(self._collect(future))

    def remove(self, ref: str, *, force: bool = False) -> RemoveView:
        """Forget a node — its model, its join record, its entry in the file and every
        binding it takes part in (06-cykl-zycia-urzadzenia.md §6).

        Ordinary removal sends `FactoryReset` and forgets **only once the node
        acknowledged it**: silence or a refusal is an error, and nothing changes. Forced
        removal sends the reset too but does not wait — for a module that is dead or
        already off the wall, the case zigbee2mqtt calls "force remove". The hub then
        keeps hearing the address until the node lets go of it, so nobody else gets it
        (03-adresacja.md §6).
        """
        future: Future[RemoveView] = Future()

        def task() -> None:
            if not future.set_running_or_notify_cancel():
                return
            inst = self.core.installation
            try:
                uid = self._find(inst, ref)
                node = inst.nodes.get(uid)
                if node is not None and self.core.enumeration.busy(node.short_id):
                    # Enumeration would finish after us and put the model back.
                    raise ApiError(
                        Problem.CONFLICT, f"{ref}: it is being enumerated — try again after"
                    )
                if not force and (node is None or not node.address):
                    raise ApiError(
                        Problem.UNAVAILABLE,
                        f"{ref}: the hub cannot reach it to reset it — remove it with force",
                    )
            except BaseException as exc:
                future.set_exception(exc)
                return

            if force:
                if node is not None and node.address:
                    self._reset(node, self._late_acknowledgement(node.address))
                future.set_result(self._forget(uid, acknowledged=None))
                return

            def done(outcome: Outcome) -> None:
                try:
                    try:
                        _outcome(outcome)
                    except ApiError as refused:
                        raise ApiError(
                            refused.problem,
                            f"{ref}: {refused.message} — nothing was removed; if the "
                            f"module is gone, remove it with force",
                            result=refused.result,
                        ) from None
                    view = self._forget(uid, acknowledged=True)
                except BaseException as exc:
                    if not future.done():
                        future.set_exception(exc)
                    return
                if not future.done():
                    future.set_result(view)

            assert node is not None
            self._reset(node, done)

        self.core.submit(task)
        return self._collect(future)

    def enumerate(self, ref: str) -> EnumerateView:
        """Read the node again — identity and descriptor — without waiting for a firmware
        change (06-cykl-zycia-urzadzenia.md §3.3: "the operator can force it").

        The model the hub holds **stays until the new one replaces it**: a read that
        fails half way leaves the node working as it was, and the failure is reported.
        """

        def order(inst: Installation) -> EnumerateView:
            node = self._present(inst, ref)
            if self.core.enumeration.busy(node.short_id):
                raise ApiError(Problem.CONFLICT, f"{ref}: enumeration is already running")
            record = inst.records.get(node.short_id)
            if record is None:
                record = inst.record(node.short_id)
                record.address, record.bus, record.uid = node.address, node.bus, node.uid
            started = self.core.enumeration.start(record)
            return EnumerateView(started=started, running=len(self.core.enumeration))

        return self._ask(order)

    def read_all(self, ref: str, endpoint: int | None = None) -> ReadoutView:
        """Read every readable attribute of the node — or of one endpoint — from the node
        into the hub's memory (zadania/zrobione/FEAT-06.md).

        Returns at once, like `enumerate`: the reads go one after another in the
        background, and each reply lands in the model the way a single `read` does. What
        the endpoint does not have is not asked. The end arrives as a `READOUT_DONE`
        event; until then `node()` says how many reads are left.
        """

        def order(inst: Installation) -> ReadoutView:
            node = self._present(inst, ref)
            if self.core.enumeration.busy(node.short_id):
                # Enumeration builds a new model; reading the old one fills nothing (R5).
                raise ApiError(Problem.CONFLICT, f"{ref}: it is being enumerated — try again after")
            if self.core.readout.busy(node.uid):
                raise ApiError(Problem.CONFLICT, f"{ref}: it is already being read")
            if endpoint is not None:
                self._functional(inst, ref, endpoint)
            pending = self.core.readout.start(node, endpoint)
            return ReadoutView(started=pending > 0, pending=pending, running=len(self.core.readout))

        return self._ask_described(ref, order)

    def block(self, ref: str) -> ConfigView:
        """Tell the hub not to serve this node (06-cykl-zycia-urzadzenia.md §8).

        It goes **to the file**, like every decision about the installation, and from
        there into the model when the file is applied. A node the file does not know yet —
        one waiting as `PENDING`, say — gets an entry holding nothing but the block.
        """
        uid = self._ask(lambda inst: self._known(inst, ref))
        path = ("nodes", bytes(uid).hex(), "blocked")
        # The current digest, not one the caller saw: the change is computed from the
        # file as it is now, so there is no stale view of it to protect.
        return self._write(lambda store: store.set(path, True, etag=store.digest()))

    def unblock(self, ref: str) -> ConfigView:
        """Undo `block`. An entry `block` created with nothing else in it goes with it —
        otherwise unblocking a stranger would leave an empty entry behind."""
        uid = self._ask(lambda inst: self._known(inst, ref))
        key = bytes(uid).hex()

        def change(store: ConfigStore) -> Config:
            nodes = store.document().get("nodes") or {}
            entry = next((v for k, v in nodes.items() if str(k).lower() == key), None)
            if not isinstance(entry, Mapping) or entry.get("blocked") is not True:
                raise ApiError(Problem.INVALID, f"{ref} is not blocked")
            alone = set(entry) == {"blocked"}
            path = ("nodes", key) if alone else ("nodes", key, "blocked")
            return store.delete(path, etag=store.digest())

        return self._write(change)

    def command(
        self, ref: str, endpoint: int, name: str, args: Mapping[str, Any] | None = None
    ) -> ActionView:
        """An addressed command. It goes **straight to the node** and does not touch the
        file."""

        def assemble(inst: Installation) -> tuple[NodeModel, int, bytes]:
            node, model, definition = self._functional(inst, ref, endpoint)
            meta = self._named(definition.command, name, "command")
            if meta.feature is not None and not model.features & meta.feature:
                raise ApiError(
                    Problem.INVALID,
                    f"{definition.name}.{name} needs a feature this endpoint does not have",
                )
            return node, meta.id, self._args(meta.args, args or {}, model)

        return self._act(ref, assemble, endpoint, MessageKind.COMMAND)

    def write(self, ref: str, endpoint: int, attribute: str, value: Any) -> ActionView:
        """A working setting. **This is not configuration** — configuration goes to the
        file and only from there to the node
        ([`KONFIGURACJA.md`](../KONFIGURACJA.md) §11.1)."""

        def assemble(inst: Installation) -> tuple[NodeModel, int, bytes]:
            node, model, definition = self._functional(inst, ref, endpoint)
            meta = self._named(definition.attribute, attribute, "attribute")
            if not meta.writable:
                raise ApiError(Problem.INVALID, f"{meta.name} is read-only")
            if meta.type == "string":
                # A text: no coercion, no range — its limit is the field's, in bytes. The
                # route onto the bus makes it a LONG_WRITE (core/hub.py).
                self._exposed(model, meta, None, ref, endpoint)
                return node, meta.id, self._encode_text(meta, value, model)
            encoded, coerced = self._encode(meta, value, model)
            self._exposed(model, meta, coerced, ref, endpoint)
            return node, meta.id, encoded

        return self._act(ref, assemble, endpoint, MessageKind.WRITE)

    def read(self, ref: str, endpoint: int, attribute: str) -> ActionView:
        """A read **from the node**, not from the hub’s memory. `node()` serves the
        memory."""

        def assemble(inst: Installation) -> tuple[NodeModel, int, bytes]:
            node, model, definition = self._functional(inst, ref, endpoint)
            meta = self._named(definition.attribute, attribute, "attribute")
            if not meta.readable:
                raise ApiError(Problem.INVALID, f"{meta.name} is write-only")
            # What the endpoint does not have is not asked for — the same answer `write`
            # gives, now that a refused read is remembered too (zadania/zrobione/FEAT-06.md, R9).
            self._exposed(model, meta, None, ref, endpoint)
            return node, meta.id, b""

        return self._act(ref, assemble, endpoint, MessageKind.READ)

    def identify(self, ref: str, seconds: int = 5) -> ActionView:
        """Which of those forty boxes. The only operation nothing else can replace while
        installing."""

        def assemble(inst: Installation) -> tuple[NodeModel, int, bytes]:
            node = self._present(inst, ref)
            return node, dev.IDENTIFY, encode("u16", seconds)

        return self._act(ref, assemble, 0, MessageKind.COMMAND)

    # --- configuration -----------------------------------------------------------

    def config(self) -> ConfigView:
        """The whole configuration with an `ETag`. A write requires handing that same `ETag`
        back."""
        return self._ask(lambda inst: self._config(inst))

    def config_set(self, path: str, value: Any, *, etag: str) -> ConfigView:
        """Write a value to the **file**, not to the node.

        It reaches the node through reconciling (§8), and that is the only route: a
        second one would make the file stop describing the installation and start
        describing whatever somebody once wrote in it
        ([`KONFIGURACJA.md`](../KONFIGURACJA.md) §11.1).
        """
        return self._write(lambda store: store.set(_segments(path), value, etag=etag))

    def config_delete(self, path: str, *, etag: str) -> ConfigView:
        return self._write(lambda store: store.delete(_segments(path), etag=etag))

    def diff(self) -> tuple[DiffRow, ...]:
        """How the installation differs from the file. Empty means "everything agrees"."""
        return self._ask(self._diff)

    def reconcile(self, ref: str) -> ReconcileView:
        """Check this node against the file **now**, without waiting for an occasion.

        Reconciling starts by itself after a node is identified and after every
        configuration change. This is for the case where the previous attempt failed —
        because the node was busy or silent — and a person wants to try again.
        """

        def order(inst: Installation) -> ReconcileView:
            node = inst.nodes.get(self._find(inst, ref))
            if node is None:
                raise ApiError(Problem.UNAVAILABLE, f"{ref}: the hub has not seen it yet")
            started_now = self.core.reconcile.start(node)
            return ReconcileView(started=started_now, running=len(self.core.reconcile))

        return self._ask_described(ref, order)

    def config_set_attribute(
        self, ref: str, endpoint: int, attribute: str, value: Any, *, etag: str
    ) -> ConfigView:
        """Write an endpoint's attribute to the file — **together with `type`, when it is
        not there yet**.

        The loader requires an endpoint to give a type before it gets an attribute
        ([`KONFIGURACJA.md`](../KONFIGURACJA.md) §10): without it there is no way to
        check whether `HoldThreshold` exists on that type at all. On a fresh
        configuration the first write therefore ended in an error that sounded like a
        hub fault and was an ordinary missing line.

        We take the type **from the node, not from the file** — because that is where
        the truth is, while the file is only filling up.
        """

        def collect(inst: Installation) -> tuple[str, list[tuple[tuple[str, ...], Any]]]:
            uid = self._find(inst, ref)
            _, model, definition = self._functional(inst, ref, endpoint)
            meta = self._named(definition.attribute, attribute, "attribute")
            if not (meta.writable and meta.persist):
                kind = "state" if not meta.writable else "a working setting"
                raise ApiError(Problem.INVALID, f"{meta.name} is {kind}, not configuration")
            given = value
            if meta.in_units:
                # A number in the file, not the string a form sends: `26.5`, which the
                # loader reads as °C on this endpoint (KONFIGURACJA.md §7.2). A bad value
                # is the loader's to reject, with its own words.
                with contextlib.suppress(ApiError):
                    given = _units(meta, value)
            try:
                wanted = coerce(meta, value) if not meta.in_units else None
            except ValueError:
                wanted = None  # a bad value is the loader's to reject, with its own words
            self._exposed(model, meta, wanted, ref, endpoint)
            base = f"nodes/{bytes(uid).hex()}/endpoints/{endpoint}"
            changes: list[tuple[tuple[str, ...], Any]] = []
            if not self._has_type(inst, uid, endpoint):
                changes.append((_segments(f"{base}/type"), definition.name))
            changes.append((_segments(f"{base}/{meta.name}"), given))
            return base, changes

        _, changes = self._ask(collect)
        return self._write(lambda store: store.set_many(changes, etag=etag))

    def _has_type(self, inst: Installation, uid: Uid, endpoint: int) -> bool:
        wanted = inst.wanted.get(uid)
        description = wanted.endpoints.get(endpoint) if wanted else None
        return description is not None and description.type is not None

    def config_adopt(self, ref: str, endpoint: int, attribute: str, *, etag: str) -> ConfigView:
        """Copy a value **from the node into the file**.

        The only place in the whole hub where something goes this way — and that is why
        it requires an explicit human command. The rule "the hub writes the file to the
        node, never the other way round"
        ([`KONFIGURACJA.md`](../KONFIGURACJA.md) §11.1) is about what the hub **does not
        do by itself**; the operator has every right to decide otherwise, as long as
        they do so deliberately.

        The value goes into the file **under its name**, where it has one: the file is
        to read `LIGHT`, not `1`, because a person reads the file.
        """

        def collect(inst: Installation) -> tuple[str, Plain | str]:
            _, model, definition = self._functional(inst, ref, endpoint)
            meta = self._named(definition.attribute, attribute, "attribute")
            if not (meta.writable and meta.persist):
                raise ApiError(
                    Problem.INVALID,
                    f"{meta.name} is not configuration, so there is nothing to adopt",
                )
            state = model.attributes.get(meta.id)
            if state is None or state.value is None:
                raise ApiError(
                    Problem.UNAVAILABLE,
                    f"{ref} ep{endpoint}: the hub does not know the value of {meta.name} — read it "
                    f"first",
                )
            name = render(meta, state.value)
            return meta.name, (name if name else _plain(state.value))

        attribute_name, value = self._ask(collect)
        return self.config_set_attribute(ref, endpoint, attribute_name, value, etag=etag)

    def _write(self, changed: Callable[[ConfigStore], Config]) -> ConfigView:
        def save(inst: Installation) -> ConfigView:
            store = self.core.config_store
            if store is None:
                raise ApiError(
                    Problem.UNAVAILABLE, "the hub is running without configuration files"
                )
            try:
                config = changed(store)
            except ConflictError as exc:
                raise ApiError(Problem.CONFLICT, str(exc)) from None
            except ConfigError as exc:
                raise ApiError(Problem.REJECTED, str(exc)) from None
            except OSError as exc:
                raise ApiError(Problem.TIMEOUT, f"writing the file failed: {exc}") from None
            # The file is written — only now memory and reconciling (§11.3).
            self.core.apply_config(config)
            return self._config(inst)

        return self._ask(save)

    # --- the event stream ---------------------------------------------------------

    @contextmanager
    def stream(self, depth: int = DEPTH) -> Iterator[EventStream]:
        """Live events, until the `with` block is left.

        **The only place that does not go through the CORE queue** — and it may not,
        because it does not touch the model: it reads a queue of its own that CORE only
        fills. A slow listener drops at its own end and stops nobody
        ([`core/events.py`](core/events.py)).
        """
        sub = self.core.events.subscribe(depth)
        try:
            yield Stream(sub, _event)
        finally:
            sub.close()

    @contextmanager
    def frames(self, depth: int = DEPTH) -> Iterator[TrafficStream]:
        """Live traffic on the bus.

        A stream separate from `stream()`, because it is a different question: an event
        says **what happened**, traffic says **why the state is what it is**. It shows
        retries, `tid`s, refusals and reply times
        ([`core/traffic.py`](core/traffic.py)).
        """
        sub = self.core.traffic.subscribe(depth)
        try:
            yield Stream(sub, _traffic)
        finally:
            sub.close()

    # --- building the views -------------------------------------------------------

    def _info(self, inst: Installation) -> HubInfo:
        return HubInfo(
            name="lagun-hub",
            version=__version__,
            nodes=len(inst.nodes),
            ready=sum(1 for node in inst.nodes.values() if node.address),
            issues=len(inst.issues),
            buses=tuple(bus.name for bus in self.core.buses),
        )

    def _nodes(self, inst: Installation) -> tuple[NodeSummary, ...]:
        familiar = {**{uid: None for uid in inst.wanted}, **inst.nodes}
        return tuple(
            self._summary(inst, uid, inst.nodes.get(uid)) for uid in sorted(familiar, key=bytes)
        )

    def _summary(self, inst: Installation, uid: Uid, node: NodeModel | None) -> NodeSummary:
        wanted = inst.wanted.get(uid)
        silence = None
        if node is not None and node.last_heard and node.silence_threshold:
            silence = max(0.0, self.core.clock() - node.last_heard)
        state = node.state.name if node else "UNKNOWN"
        if node is None:
            # A node the hub knows only from a join record — blocked before it was ever
            # read, say — has a state worth showing all the same.
            record = next((r for r in inst.records.values() if r.uid == uid), None)
            state = record.state.name if record else state
        return NodeSummary(
            uid=bytes(uid).hex(),
            alias=wanted.alias if wanted else "",
            name=(node.name if node else "") or (wanted.name if wanted else ""),
            room=wanted.room if wanted else "",
            address=node.address if node else 0,
            bus=self._bus_name(node.bus) if node else "",
            state=state,
            endpoints=len(node.endpoints) if node else 0,
            silent_for=silence,
            configured=wanted is not None,
            blocked=inst.is_blocked(uid),
        )

    def _node(self, inst: Installation, uid: Uid) -> NodeView:
        node = inst.nodes.get(uid)
        summary = self._summary(inst, uid, node)
        endpoints = (
            tuple(self._endpoint(inst, uid, model) for _, model in sorted(node.endpoints.items()))
            if node
            else ()
        )
        return NodeView(
            **{f.name: getattr(summary, f.name) for f in fields(NodeSummary)},
            short_id=node.short_id if node else 0,
            vendor_id=node.vendor_id if node else 0,
            device_type=node.device_type if node else 0,
            firmware=node.firmware if node else 0,
            detail=endpoints,
            reading=self.core.readout.remaining(uid),
        )

    def _endpoint(self, inst: Installation, uid: Uid, model: EndpointModel) -> EndpointView:
        definition = model.definition
        wanted = inst.wanted.get(uid)
        description = wanted.endpoints.get(model.number) if wanted else None
        quantity = _quantity(model)
        return EndpointView(
            number=model.number,
            type_id=int(model.type_id) if model.type_id is not None else None,
            type_name=model.type_id.name if model.type_id is not None else "",
            revision=model.revision,
            features=model.features,
            channel=model.channel,
            name=_wanted_name(description),
            groups=tuple(description.groups) if description else (),
            quantity=quantity.name if quantity else None,
            attributes=self._attributes(model, definition, description),
            commands=self._commands(definition, model),
            events=tuple(e.name for e in definition.events.values()) if definition else (),
            event_types=tuple(
                EventTypeView(
                    name=e.name,
                    fields=tuple(
                        ArgView(name=f.name, type=_arg_type(f, quantity)) for f in e.params
                    ),
                )
                for e in definition.events.values()
            )
            if definition
            else (),
        )

    def _commands(
        self, definition: TypeDef | None, model: EndpointModel
    ) -> tuple[CommandView, ...]:
        """Commands together with their arguments — otherwise a client knows **that** it can
        send something, but not **what** to put in it."""
        if definition is None:
            return ()
        quantity = _quantity(model)
        return tuple(
            CommandView(
                name=meta.name,
                args=tuple(
                    ArgView(
                        name=f.name,
                        type=_arg_type(f, quantity),
                        choices=(),
                        lo=(
                            SCALARS[type_name].lo
                            if (type_name := _arg_type(f, quantity)) in SCALARS
                            else None
                        ),
                        hi=SCALARS[type_name].hi if type_name in SCALARS else None,
                    )
                    for f in meta.args
                ),
                available=meta.feature is None or bool(model.features & meta.feature),
            )
            for meta in definition.commands.values()
        )

    def _attributes(
        self, model: EndpointModel, definition: TypeDef | None, description: EndpointWanted | None
    ) -> tuple[AttributeView, ...]:
        if definition is None:
            # An unidentified endpoint: we show the raw bytes the hub cannot name.
            # Better than pretending they are not there.
            return tuple(
                AttributeView(
                    id=number,
                    name=f"0x{number:02X}",
                    type="",
                    access="",
                    raw=state.raw.hex(),
                    age=self._age(state),
                )
                for number, state in sorted(model.attributes.items())
            )
        wanted_values = description.attributes if description else {}
        return tuple(
            self._attribute(
                model,
                meta,
                model.attributes.get(number),
                wanted_values.get(number),
                model.lacks(meta),
            )
            for number, meta in sorted(definition.attributes.items())
        )

    def _attribute(
        self,
        model: EndpointModel,
        meta: AttributeDef,
        state: AttributeState | None,
        wanted: Wanted | None,
        missing: str | None = None,
    ) -> AttributeView:
        value = _plain(state.value) if state else None
        if meta.type == "string" and state is not None:
            value = _text(state.raw)
        desired, desired_label = _desired(model, meta, wanted)
        return AttributeView(
            id=meta.id,
            name=meta.name,
            type=meta.type,
            access=meta.access,
            value=value,
            label=_label(model, meta, state.value if state else None),
            raw=state.raw.hex() if state else "",
            age=self._age(state),
            wanted=_plain(desired),
            wanted_label=desired_label,
            matches=None if desired is None or state is None else state.value == desired,
            choices=choices(meta),
            configurable=meta.writable and meta.persist and missing is None,
            available=missing is None,
            reported=meta.reported,
        )

    def _age(self, state: AttributeState | None) -> float | None:
        return max(0.0, self.core.clock() - state.at) if state else None

    def _bindings(self, inst: Installation) -> tuple[BindingView, ...]:
        aliases = {uid: w.alias for uid, w in inst.wanted.items()}
        out: dict[tuple[Uid, int, int, bool], list[TargetView]] = {}
        labels: dict[tuple[Uid, int, int, bool], str] = {}
        for spec in inst.bindings:
            uid, endpoint, element = spec.source
            key = (uid, endpoint, element, spec.on_report)
            labels.setdefault(key, spec.source_label)
            cel = inst.nodes.get(spec.target_uid) if spec.target_uid else None
            out.setdefault(key, []).append(
                TargetView(
                    kind=spec.kind.name,
                    endpoint=spec.target_endpoint,
                    element=spec.target_element,
                    uid=bytes(spec.target_uid).hex() if spec.target_uid else "",
                    alias=aliases.get(spec.target_uid, "") if spec.target_uid else "",
                    group=spec.group,
                    label=spec.label,
                    active=bool(inst.group_buses(spec.group))
                    if spec.group is not None
                    else bool(cel and cel.address),
                )
            )
        return tuple(
            BindingView(
                source_uid=bytes(uid).hex(),
                source_alias=aliases.get(uid, ""),
                source_endpoint=endpoint,
                source_element=element,
                on_report=on_report,
                label=labels[(uid, endpoint, element, on_report)],
                targets=tuple(targets),
            )
            for (uid, endpoint, element, on_report), targets in out.items()
        )

    def _commissioning(self, inst: Installation) -> CommissioningView:
        return CommissioningView(
            accepting=self.core.commissioning.accepting,
            known=tuple(
                JoinView(
                    short_id=record.short_id,
                    node_class=record.node_class.name,
                    address=record.address,
                    bus=record.bus,
                    state=record.state.name,
                    uid=bytes(record.uid).hex() if record.uid else None,
                    last_seen=record.last_seen or None,
                )
                for record in sorted(inst.records.values(), key=lambda r: r.short_id)
            ),
        )

    def _health(self, inst: Installation) -> HealthView:
        core = self.core
        tx = core.transactions
        rec = core.reconcile
        engine = self.engine
        return HealthView(
            nodes=len(inst.nodes),
            ready=sum(1 for node in inst.nodes.values() if node.address),
            configured=len(inst.wanted),
            issues=len(inst.issues),
            buses=tuple(self._bus(i, bus.name) for i, bus in enumerate(core.buses)),
            uptime=core.clock() - core.started,
            latency=engine.latency.percentiles() if engine else {},
            bindings={
                "matched": engine.matched if engine else 0,
                "unmatched": engine.unmatched if engine else 0,
                "sent": engine.sent if engine else 0,
                "dropped": engine.dropped if engine else 0,
                "configured": len(inst.bindings),
            },
            core={
                "handled": core.handled,
                "ignored": core.ignored,
                "published": core.published,
                "dropped_sends": core.dropped_sends,
                "unmatched_responses": core.unmatched_responses,
                "strangers_probed": core.strangers.probed,
                "strangers_pending": core.strangers.pending,
                "strangers_failed": core.strangers.failed,
            },
            transactions={
                "opened": tx.opened,
                "answered": tx.answered,
                "retried": tx.retried,
                "lost": tx.lost,
            },
            reconcile={
                "checked": rec.checked,
                "mismatched": rec.mismatched,
                "written": rec.written,
                "failed": rec.failed,
                "running": len(rec),
            },
        )

    def _planned(self, channel: str) -> AbstractContextManager[None]:
        return self.watch.planned(channel) if self.watch is not None else nullcontext()

    def _link(self, channel: str) -> tuple[str, str]:
        """The watch's word on a bus — empty for one it does not watch."""
        state = self.watch.state(channel) if self.watch is not None else None
        return (state.link.value, state.fault) if state is not None else ("", "")

    def _bus(self, index: int, name: str) -> BusView:
        reading = self.core.meter.reading(index)
        bitrate = _bitrate(self.core.config, name)
        link, fault = self._link(name)
        return BusView(
            index=index,
            name=name,
            rx=reading.rx,
            tx=reading.tx,
            dropped=reading.dropped,
            rx_rate=reading.rx_rate,
            tx_rate=reading.tx_rate,
            bitrate=bitrate,
            load=reading.bits_rate / bitrate if bitrate else None,
            link=link,
            fault=fault,
        )

    def _config(self, inst: Installation) -> ConfigView:
        del inst
        store = self.core.config_store
        if store is None:
            return ConfigView(etag="", files=(), document={})
        return ConfigView(
            etag=store.etag,
            files=tuple(file_.name for file_ in store.files()),
            document=_jsonable(store.document()),
        )

    def _diff(self, inst: Installation) -> tuple[DiffRow, ...]:
        out: list[DiffRow] = []
        for uid, wanted in inst.wanted.items():
            node = inst.nodes.get(uid)
            for number, description in wanted.endpoints.items():
                model = node.endpoints.get(number) if node else None
                definition = model.definition if model else None
                for element, desired in description.attributes.items():
                    meta = definition.attributes.get(element) if definition else None
                    if meta is None:
                        out.append(
                            DiffRow(
                                uid=bytes(uid).hex(),
                                alias=wanted.alias,
                                endpoint=number,
                                attribute=f"0x{element:02X}",
                                state="missing",
                                wanted=_plain(desired),
                            )
                        )
                        continue
                    current = model.attributes.get(element) if model else None
                    wanted_value, wanted_label = (
                        _desired(model, meta, desired) if model else (None, None)
                    )
                    if current is None:
                        state = "unknown"
                    elif current.value == wanted_value:
                        state = "match"
                    else:
                        state = "mismatch"
                    out.append(
                        DiffRow(
                            uid=bytes(uid).hex(),
                            alias=wanted.alias,
                            endpoint=number,
                            attribute=meta.name,
                            state=state,
                            wanted=_plain(wanted_value),
                            wanted_label=wanted_label,
                            actual=_plain(current.value) if current else None,
                            actual_label=_label(model, meta, current.value)
                            if model and current
                            else None,
                        )
                    )
        return tuple(out)

    # --- shared -------------------------------------------------------------------

    def _bus_entries(
        self, inst: Installation
    ) -> tuple[tuple[BusConfig, ...], frozenset[str], dict[str, int]]:
        """On CORE: the file's buses, the open ones by name, nodes per open bus."""
        config = self.core.config
        names = [bus.name for bus in self.core.buses]
        counts: dict[str, int] = {}
        for node in inst.nodes.values():
            if node.address and 0 <= node.bus < len(names):
                counts[names[node.bus]] = counts.get(names[node.bus], 0) + 1
        return (config.buses if config else ()), frozenset(names), counts

    @staticmethod
    def _bus_entry(entries: tuple[BusConfig, ...], kind: str, name: str) -> BusConfig:
        for bus in entries:
            if bus.name == name and bus.kind.value == kind:
                return bus
        raise ApiError(Problem.NOT_FOUND, f"there is no {kind} bus {name!r} in hub.yaml")

    def _setup(
        self, bus: BusConfig, open_channels: frozenset[str], counts: dict[str, int]
    ) -> BusSetupView:
        """Ask the driver — off CORE. A driver that cannot even look is a state of the
        bus, not a failure of the list: one unplugged adapter must not hide the rest."""
        # An open `SocketCanBus` is named after its channel (`_bitrate` relies on it too).
        base = BusSetupView(
            name=bus.name,
            kind=bus.kind.value,
            channel=bus.channel,
            bitrate=bus.bitrate,
            open=bus.channel in open_channels,
            nodes=counts.get(bus.channel, 0),
            link=self._link(bus.channel)[0],
            fault=self._link(bus.channel)[1],
        )
        driver = self.drivers(bus)
        try:
            driver.open()
            try:
                info = driver.info()
                plan = driver.plan(bus_setup.settings(bus))
            finally:
                driver.close()
        except (DriverError, ValueError) as exc:
            return replace(base, state="error", error=str(exc))
        if any(not change.manual for change in plan):
            state = "drift"
        elif plan:
            state = "manual"
        else:
            state = "ok"
        return replace(
            base,
            hardware=info.firmware,
            where=info.where,
            state=state,
            changes=tuple(_change(change) for change in plan),
        )

    def _bus_name(self, index: int) -> str:
        buses = self.core.buses
        return buses[index].name if 0 <= index < len(buses) else str(index)

    def _find(self, inst: Installation, ref: str) -> Uid:
        """A node by `Uid`, alias or name. All three, because people use all three — and the
        hub reduces them to one anyway."""
        sought = ref.strip()
        try:
            candidate = Uid(bytes.fromhex(sought))
        except ValueError:
            candidate = None
        if candidate is not None and (candidate in inst.nodes or candidate in inst.wanted):
            return candidate
        for uid, wanted in inst.wanted.items():
            if wanted.alias == sought or wanted.name == sought:
                return uid
        for uid, node in inst.nodes.items():
            if node.name == sought:
                return uid
        raise ApiError(Problem.NOT_FOUND, f"there is no node {ref!r}")

    def _stranger(self, inst: Installation, short_id: int) -> JoinRecord:
        """A pending node **with an address** — one found by `Strangers`. A pending node
        without one is waiting to join, and the accepting mode is its road in."""
        record = inst.records.get(short_id)
        if record is None:
            raise ApiError(Problem.NOT_FOUND, f"the hub has never seen ShortId 0x{short_id:08X}")
        if record.state != NodeState.PENDING:
            raise ApiError(
                Problem.INVALID, f"ShortId 0x{short_id:08X} is {record.state.name}, not PENDING"
            )
        if not record.address:
            raise ApiError(
                Problem.INVALID,
                f"ShortId 0x{short_id:08X} has no address — it is waiting to join; "
                f"open the accepting mode instead",
            )
        return record

    def _present(self, inst: Installation, ref: str) -> NodeModel:
        """A node one can talk to. Configured but absent — one cannot; blocked — one
        does not (06-cykl-zycia-urzadzenia.md §8)."""
        uid = self._find(inst, ref)
        node = inst.nodes.get(uid)
        if node is None:
            raise ApiError(Problem.UNAVAILABLE, f"{ref}: the hub has not seen it yet")
        if inst.is_blocked(uid):
            raise ApiError(Problem.UNAVAILABLE, f"{ref}: is blocked — unblock it first")
        if not node.address:
            raise ApiError(Problem.UNAVAILABLE, f"{ref}: has no address")
        return node

    def _known(self, inst: Installation, ref: str) -> Uid:
        """`_find`, plus the nodes only a join record knows by `Uid` — a stranger waiting
        as `PENDING`, which the operator may want to block rather than admit."""
        try:
            return self._find(inst, ref)
        except ApiError:
            sought = ref.strip().lower()
            for record in inst.records.values():
                if record.uid is not None and bytes(record.uid).hex() == sought:
                    return record.uid
            raise

    def _reset(self, node: NodeModel, done: Callable[[Outcome], None]) -> None:
        self.core.transact(
            node.bus,
            node.address,
            0,
            dev.FACTORY_RESET,
            MessageKind.COMMAND,
            encode("u32", dev.FACTORY_RESET_MAGIC),
            done,
        )

    def _late_acknowledgement(self, address: int) -> Callable[[Outcome], None]:
        """A forced removal did not wait — but if the node acknowledges after all, its
        address is free on the bus too, exactly as after an ordinary removal."""

        def done(outcome: Outcome) -> None:
            if outcome.ok and self.core.installation.holder(address) is None:
                self.core.strangers.forget(address)

        return done

    def _forget(self, uid: Uid, *, acknowledged: bool | None) -> RemoveView:
        """From the CORE thread: the model, then the file.

        A file that cannot be changed does not undo the removal — the node has already
        been reset, or (forced) let go of. It is reported, and the entry is left for a
        person to delete.
        """
        node = self.core.forget(uid, acknowledged=acknowledged is True)
        unconfigured, bindings = False, 0
        store = self.core.config_store
        if store is not None and uid in self.core.installation.wanted:
            try:
                config, bindings = store.forget_node(uid, etag=store.digest())
            except (ConfigError, OSError) as exc:
                self.core.installation.issues.append(
                    f"{bytes(uid).hex()} was removed, but its entry could not leave the "
                    f"configuration file: {exc} — delete it by hand"
                )
            else:
                unconfigured = True
                self.core.apply_config(config)
        return RemoveView(
            uid=bytes(uid).hex(),
            acknowledged=acknowledged,
            address=node.address if node is not None else 0,
            unconfigured=unconfigured,
            bindings=bindings,
        )

    def _functional(
        self, inst: Installation, ref: str, endpoint: int
    ) -> tuple[NodeModel, EndpointModel, TypeDef]:
        node = self._present(inst, ref)
        model = node.endpoints.get(endpoint)
        if model is None:
            familiar = ", ".join(f"ep{n}" for n in sorted(node.endpoints)) or "none"
            raise ApiError(Problem.NOT_FOUND, f"{ref} has no ep{endpoint}; it has: {familiar}")
        definition = model.definition
        if definition is None:
            raise ApiError(
                Problem.UNAVAILABLE,
                f"{ref} ep{endpoint}: the hub does not know this endpoint's type yet",
            )
        return node, model, definition

    @staticmethod
    def _exposed(
        model: EndpointModel, meta: AttributeDef, value: Value | None, ref: str, endpoint: int
    ) -> None:
        """Refuse what this endpoint cannot have — **before** the file or the node.

        The hub knows the features from enumeration and remembers the optional
        attributes a node turned down, so it answers here rather than letting the
        request go out and come back refused (01-model-danych.md §2.1, `LB-HUB-026`).
        """
        reason = model.lacks(meta, value)
        if reason is not None:
            raise ApiError(Problem.INVALID, f"{ref} ep{endpoint}: {reason}")

    def _named[T](self, lookup: Callable[[str], T], name: str, what: str) -> T:
        try:
            return lookup(name)
        except ValueError as exc:
            raise ApiError(Problem.NOT_FOUND, str(exc)) from None

    def _encode(self, meta: AttributeDef, value: Any, model: EndpointModel) -> tuple[bytes, Value]:
        """The bytes of a write and the value they carry. One in units — a threshold, a
        display's number — is given as a person reads it, `21.37` for a temperature, the
        same as the file and the view (zadania/zrobione/DEVICE-01.md N5, N12)."""
        if meta.in_units:
            type_name = _raw_type(model, meta) if meta.type == "<Q>" else meta.type
            try:
                coerced = model.on_wire(meta, _units(meta, value))
            except ValueError as exc:
                raise ApiError(Problem.INVALID, str(exc)) from None
            return encode(type_name, coerced), coerced
        try:
            coerced = coerce(meta, value)
        except ValueError as exc:
            raise ApiError(Problem.INVALID, str(exc)) from None
        type_name = meta.type if meta.type != "<Q>" else _raw_type(model, meta)
        if not in_range(type_name, coerced):
            raise ApiError(Problem.INVALID, f"{meta.name} = {coerced} is outside {type_name}")
        return encode(type_name, coerced), coerced

    @staticmethod
    def _encode_text(meta: AttributeDef, value: Any, model: EndpointModel) -> bytes:
        """UTF-8 with its prefix, refused here when it is longer than the field holds.

        `TextLength` counts **bytes** (types/0x0022-display.md §5.3) — "Pranie skończone"
        is sixteen letters and seventeen bytes. The hub has read it at enumeration, so it
        refuses rather than letting the node do it; unknown, it sends and the node
        decides."""
        try:
            encoded = encode_string(str(value))
        except ValueError as exc:
            raise ApiError(Problem.INVALID, f"{meta.name}: {exc}") from None
        limit = _text_length(model)
        if limit is not None and len(encoded) - STRING_PREFIX > limit:
            raise ApiError(
                Problem.INVALID,
                f"{meta.name} is {len(encoded) - STRING_PREFIX} bytes; "
                f"the field holds {limit} (TextLength)",
            )
        return encoded

    def _args(
        self, spec: tuple[Field, ...], args: Mapping[str, Any], model: EndpointModel
    ) -> bytes:
        try:
            return encode_fields(spec, dict(args), quantity=_quantity(model))
        except ValueError as exc:
            raise ApiError(Problem.INVALID, str(exc)) from None

    # --- the road to CORE --------------------------------------------------------

    def _ask[T](self, build: Callable[[Installation], T]) -> T:
        """Run something **on the CORE thread** and bring the result back.

        Reads included: the model belongs to CORE and reaching into it from the side is
        a mistake, however briefly it may look like it works.
        """
        if self.core.ident is not None and not self.core.is_alive():
            # Started and gone: nobody reads the queue, so waiting out the timeout would
            # only hide why (zadania/zrobione/BUG-04.md). Tests drive CORE with pump() and never
            # start it — for them `ident` stays None.
            raise ApiError(Problem.TIMEOUT, "the hub's CORE thread has stopped — restart the hub")
        future: Future[T] = Future()

        def task() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(build(self.core.installation))
            except BaseException as exc:
                future.set_exception(exc)

        self.core.submit(task)
        return self._collect(future)

    def _act(
        self,
        ref: str,
        build: Callable[[Installation], tuple[NodeModel, int, bytes]],
        endpoint: int,
        kind: MessageKind,
    ) -> ActionView:
        """A transaction: CORE assembles the request and the reply comes back the same way."""
        future: Future[Outcome] = Future()

        def send() -> None:
            try:
                node, element, args = build(self.core.installation)
            except BaseException as exc:
                future.set_exception(exc)
                return
            self.core.request(
                node.address,
                endpoint,
                element,
                kind=kind,
                args=args,
                on_done=future.set_result,
            )

        def task() -> None:
            if future.set_running_or_notify_cancel():
                self._described(ref, send, future.set_exception)

        self.core.submit(task)
        return _outcome(self._collect(future))

    def _ask_described[T](self, ref: str, build: Callable[[Installation], T]) -> T:
        """`_ask`, for an operation that needs the node's endpoints — run once they are
        read."""
        future: Future[T] = Future()

        def run() -> None:
            try:
                future.set_result(build(self.core.installation))
            except BaseException as exc:
                future.set_exception(exc)

        def task() -> None:
            if future.set_running_or_notify_cancel():
                self._described(ref, run, future.set_exception)

        self.core.submit(task)
        return self._collect(future)

    def _described(
        self, ref: str, run: Callable[[], None], fail: Callable[[BaseException], None]
    ) -> None:
        """From CORE: `run`, once the hub has read what this node is.

        A node taken from the file after a restart is reachable at once, but its
        endpoints are read only at its first frame (zadania/zrobione/BUG-05.md, N9). An operation
        that comes first does not stop at "the hub does not know this endpoint yet" —
        it reads the descriptor, one node, and goes on. A node the hub cannot reach is
        `run`'s business: whatever it raises is the right answer.
        """
        try:
            node = self._present(self.core.installation, ref)
        except ApiError:
            run()
            return
        if node.described:
            run()
            return

        def done(ready: bool) -> None:
            if ready:
                run()
            else:
                fail(
                    ApiError(
                        Problem.SILENT,
                        f"{ref}: reading what the node is failed — it did not answer",
                    )
                )

        self.core.describe(node, done)

    def _collect[T](self, future: Future[T]) -> T:
        try:
            result: T = self.wait(future, self.timeout)
        except FutureTimeout:
            future.cancel()
            raise ApiError(
                Problem.TIMEOUT, f"the hub did not answer within {self.timeout:.1f} s"
            ) from None
        return result


# --- small change ----------------------------------------------------------------


def _outcome(outcome: Outcome) -> ActionView:
    if not outcome.answered:
        raise ApiError(Problem.SILENT, f"the node did not answer after {outcome.attempts} attempts")
    if outcome.result != ResultCode.OK:
        name = outcome.result.name if outcome.result else None
        raise ApiError(Problem.REFUSED, f"the node refused: {name}", result=name)
    return ActionView(
        ok=True,
        result=outcome.result.name,
        attempts=outcome.attempts,
        elapsed=outcome.elapsed,
        data=outcome.data.hex(),
    )


def _change(change: Change) -> ChangeView:
    return ChangeView(
        what=change.what,
        current=change.current,
        desired=change.desired,
        how=change.how,
        manual=change.manual,
    )


def _bitrate(config: Config | None, name: str) -> int | None:
    """The bitrate of an open bus, from the file.

    By the **channel**, because that is what an open `SocketCanBus` is named after; the
    name from the file is only a label. Looked up on every reading rather than once at
    start, because the file may be reloaded with a new bitrate.
    """
    if config is None:
        return None
    for bus in config.buses:
        if bus.channel == name:
            return bus.bitrate
    return None


def _wanted_name(description: EndpointWanted | None) -> str:
    """`EndpointWanted` does not promise a name — only `EndpointConfig` does."""
    return getattr(description, "name", "") if description is not None else ""


def _segments(path: str) -> tuple[str, ...]:
    parts = tuple(part for part in path.strip("/").split("/") if part)
    if not parts:
        raise ApiError(Problem.INVALID, "the write path cannot be empty")
    return parts


def _jsonable(value: Any) -> Any:
    """`ruamel` returns map and list types of its own — plain ones go into JSON."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _event(item: Event) -> EventView:
    return EventView(
        kind=item.kind.value,
        at=item.at,
        uid=bytes(item.uid).hex() if item.uid else "",
        endpoint=item.endpoint,
        element=item.element,
        name=item.name,
        value=_plain(item.value),
        raw=item.raw.hex(),
        text=item.text,
    )


def _traffic(item: Traffic) -> TrafficView:
    return TrafficView(
        at=item.at,
        bus=item.bus,
        kind=item.kind,
        outgoing=item.outgoing,
        address=item.address,
        endpoint=item.endpoint,
        element=item.element,
        uid=bytes(item.uid).hex() if item.uid else "",
        name=item.name,
        detail=item.detail,
        tid=item.tid,
        result=item.result,
        elapsed=item.elapsed,
        late=item.late,
        retry=item.retry,
        data=item.data.hex(),
    )


def _label(model: EndpointModel, meta: AttributeDef, value: Value | None) -> str | None:
    """How a person reads a value: `21.37 °C` for one in units, `LIGHT` for a named one.
    "Absent" and unknown have none — the view draws `—`, never a zero
    (types/0x0030-sense.md §7; zadania/zrobione/DEVICE-01.md N12)."""
    if value is None:
        return None
    if meta.in_units:
        return model.in_units(meta, value)
    return render(meta, value)


def _desired(
    model: EndpointModel, meta: AttributeDef, wanted: Wanted | None
) -> tuple[Value | None, str | None]:
    """The file's value as the node would hold it, and its label. One in units the hub
    cannot convert yet — the quantity unread, or out of its range — is shown as the
    file says it, and compares with nothing."""
    if wanted is None:
        return None, None
    try:
        value = model.on_wire(meta, wanted)
    except ValueError:
        return None, f"{wanted:g}"
    return value, _label(model, meta, value)


def _plain(value: Wanted | None) -> Plain:
    if value is None or value is ABSENT:
        return None
    return value if isinstance(value, bool | int) else None


def _arg_type(field: Field, quantity: Quantity | None) -> str:
    try:
        return field_type(field, quantity)
    except ValueError:
        return field.type


def _text_length(model: EndpointModel) -> int | None:
    """`TextLength` of a text field as the hub last saw it, or `None`."""
    definition = model.definition
    if definition is None:
        return None
    try:
        number = definition.attribute("TextLength").id
    except ValueError:
        return None
    state = model.attributes.get(number)
    if state is None or not isinstance(state.value, int) or isinstance(state.value, bool):
        return None
    return state.value


def _text(raw: bytes) -> str | None:
    """A string attribute's bytes as text — a glyph that is not UTF-8 shown as the
    replacement character, as a field would draw `?` (types/0x0022-display.md §5.3)."""
    if not raw or len(raw) < STRING_PREFIX + raw[0]:
        return None
    return raw[STRING_PREFIX : STRING_PREFIX + raw[0]].decode("utf-8", errors="replace")


def _quantity(model: EndpointModel) -> Quantity | None:
    """The quantity of a `Sense` or a `Display` field — without it `Value` cannot be
    decoded. Found by name: the two types happen to number it alike, and nothing says
    the next one will."""
    definition = model.definition
    try:
        number = definition.attribute("Quantity").id if definition is not None else sns.QUANTITY
    except ValueError:
        return None
    state = model.attributes.get(number)
    if state is None or not isinstance(state.value, int) or isinstance(state.value, bool):
        return None
    try:
        return Quantity(state.value)
    except ValueError:
        return None


def _units(meta: AttributeDef, value: Any) -> float:
    """A number in the quantity's units, from what a person or a form gave."""
    if not isinstance(value, bool):
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    raise ApiError(Problem.INVALID, f"{meta.name} wants a number in units, not {value!r}")


def _raw_type(model: EndpointModel, meta: AttributeDef) -> str:
    from lagunhub.protocol.quantities import raw_type

    quantity = _quantity(model)
    if quantity is None:
        raise ApiError(
            Problem.UNAVAILABLE,
            f"{meta.name} depends on a quantity the hub has not read yet",
        )
    return raw_type(quantity)


__all__ = ["EventStream", "HubApi", "Stream", "TrafficStream", "Waiter", "block"]
