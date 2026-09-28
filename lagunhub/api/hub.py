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

from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import fields
from typing import Any, Protocol

from lagunhub import __version__
from lagunhub.api.errors import ApiError, Problem
from lagunhub.api.views import (
    ActionView,
    ArgView,
    AttributeView,
    BindingView,
    BusView,
    CommandView,
    CommissioningView,
    ConfigView,
    DiffRow,
    EndpointView,
    EventView,
    HealthView,
    HubInfo,
    JoinView,
    NodeSummary,
    NodeView,
    Plain,
    ReconcileView,
    TargetView,
    TrafficView,
)
from lagunhub.core.bindings import BindingEngine
from lagunhub.core.config.loader import ConfigError
from lagunhub.core.config.model import Config
from lagunhub.core.config.store import ConfigStore, ConflictError
from lagunhub.core.events import DEPTH, Event, Subscription
from lagunhub.core.hub import CoreThread
from lagunhub.core.model import (
    AttributeState,
    EndpointModel,
    EndpointWanted,
    Installation,
    NodeModel,
    Uid,
)
from lagunhub.core.traffic import Traffic
from lagunhub.core.transactions import Outcome
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
from lagunhub.protocol.values import ABSENT, SCALARS, Value, encode, in_range

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
    ) -> None:
        self.core = core
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

    # --- control -----------------------------------------------------------------

    def accept_new(self, seconds: float = 900.0) -> CommissioningView:
        """Open the joining window. It expires on its own — there is no "forever" mode."""

        def open_bus(inst: Installation) -> CommissioningView:
            self.core.commissioning.accept_new(seconds)
            return self._commissioning(inst)

        return self._ask(open_bus)

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

        return self._act(assemble, endpoint, MessageKind.COMMAND)

    def write(self, ref: str, endpoint: int, attribute: str, value: Any) -> ActionView:
        """A working setting. **This is not configuration** — configuration goes to the
        file and only from there to the node
        ([`KONFIGURACJA.md`](../KONFIGURACJA.md) §11.1)."""

        def assemble(inst: Installation) -> tuple[NodeModel, int, bytes]:
            node, model, definition = self._functional(inst, ref, endpoint)
            meta = self._named(definition.attribute, attribute, "attribute")
            if not meta.writable:
                raise ApiError(Problem.INVALID, f"{meta.name} is read-only")
            return node, meta.id, self._encode(meta, value, model)

        return self._act(assemble, endpoint, MessageKind.WRITE)

    def read(self, ref: str, endpoint: int, attribute: str) -> ActionView:
        """A read **from the node**, not from the hub’s memory. `node()` serves the
        memory."""

        def assemble(inst: Installation) -> tuple[NodeModel, int, bytes]:
            node, _, definition = self._functional(inst, ref, endpoint)
            meta = self._named(definition.attribute, attribute, "attribute")
            if not meta.readable:
                raise ApiError(Problem.INVALID, f"{meta.name} is write-only")
            return node, meta.id, b""

        return self._act(assemble, endpoint, MessageKind.READ)

    def identify(self, ref: str, seconds: int = 5) -> ActionView:
        """Which of those forty boxes. The only operation nothing else can replace while
        installing."""

        def assemble(inst: Installation) -> tuple[NodeModel, int, bytes]:
            node = self._present(inst, ref)
            return node, dev.IDENTIFY, encode("u16", seconds)

        return self._act(assemble, 0, MessageKind.COMMAND)

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

        return self._ask(order)

    def config_set_attribute(
        self, ref: str, endpoint: int, attribute: str, value: Any, *, etag: str
    ) -> ConfigView:
        """Write an endpoint's attribute to the file — **together with `type`, when it is
        not there yet**.

        The loader requires an endpoint to give a type before it gets an attribute
        ([`KONFIGURACJA.md`](../KONFIGURACJA.md) §10): without it there is no way to
        check whether `DebounceTime` exists on that type at all. On a fresh
        configuration the first write therefore ended in an error that sounded like a
        hub fault and was an ordinary missing line.

        We take the type **from the node, not from the file** — because that is where
        the truth is, while the file is only filling up.
        """

        def collect(inst: Installation) -> tuple[str, list[tuple[tuple[str, ...], Any]]]:
            uid = self._find(inst, ref)
            _, _, definition = self._functional(inst, ref, endpoint)
            meta = self._named(definition.attribute, attribute, "attribute")
            if not (meta.writable and meta.persist):
                kind = "state" if not meta.writable else "a working setting"
                raise ApiError(Problem.INVALID, f"{meta.name} is {kind}, not configuration")
            base = f"nodes/{bytes(uid).hex()}/endpoints/{endpoint}"
            changes: list[tuple[tuple[str, ...], Any]] = []
            if not self._has_type(inst, uid, endpoint):
                changes.append((_segments(f"{base}/type"), definition.name))
            changes.append((_segments(f"{base}/{meta.name}"), value))
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
        return NodeSummary(
            uid=bytes(uid).hex(),
            alias=wanted.alias if wanted else "",
            name=(node.name if node else "") or (wanted.name if wanted else ""),
            room=wanted.room if wanted else "",
            address=node.address if node else 0,
            bus=self._bus_name(node.bus) if node else "",
            state=node.state.name if node else "UNKNOWN",
            endpoints=len(node.endpoints) if node else 0,
            silent_for=silence,
            configured=wanted is not None,
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
            self._attribute(meta, model.attributes.get(number), wanted_values.get(number))
            for number, meta in sorted(definition.attributes.items())
        )

    def _attribute(
        self, meta: AttributeDef, state: AttributeState | None, wanted: Value | None
    ) -> AttributeView:
        value = _plain(state.value) if state else None
        return AttributeView(
            id=meta.id,
            name=meta.name,
            type=meta.type,
            access=meta.access,
            value=value,
            label=render(meta, state.value) if state and state.value is not None else None,
            raw=state.raw.hex() if state else "",
            age=self._age(state),
            wanted=_plain(wanted),
            wanted_label=render(meta, wanted) if wanted is not None else None,
            matches=None if wanted is None or state is None else state.value == wanted,
            choices=choices(meta),
            configurable=meta.writable and meta.persist,
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
            buses=tuple(BusView(index=i, name=bus.name) for i, bus in enumerate(core.buses)),
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
                "unknown_booted": core.unknown_booted,
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
                    if current is None:
                        state = "unknown"
                    elif current.value == desired:
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
                            wanted=_plain(desired),
                            wanted_label=render(meta, desired),
                            actual=_plain(current.value) if current else None,
                            actual_label=render(meta, current.value)
                            if current and current.value is not None
                            else None,
                        )
                    )
        return tuple(out)

    # --- shared -------------------------------------------------------------------

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

    def _present(self, inst: Installation, ref: str) -> NodeModel:
        """A node one can talk to. Configured but absent — one cannot."""
        uid = self._find(inst, ref)
        node = inst.nodes.get(uid)
        if node is None:
            raise ApiError(Problem.UNAVAILABLE, f"{ref}: the hub has not seen it yet")
        if not node.address:
            raise ApiError(Problem.UNAVAILABLE, f"{ref}: has no address")
        return node

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

    def _named[T](self, lookup: Callable[[str], T], name: str, what: str) -> T:
        try:
            return lookup(name)
        except ValueError as exc:
            raise ApiError(Problem.NOT_FOUND, str(exc)) from None

    def _encode(self, meta: AttributeDef, value: Any, model: EndpointModel) -> bytes:
        try:
            coerced = coerce(meta, value)
        except ValueError as exc:
            raise ApiError(Problem.INVALID, str(exc)) from None
        type_name = meta.type if meta.type != "<Q>" else _raw_type(model, meta)
        if not in_range(type_name, coerced):
            raise ApiError(Problem.INVALID, f"{meta.name} = {coerced} is outside {type_name}")
        return encode(type_name, coerced)

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
        build: Callable[[Installation], tuple[NodeModel, int, bytes]],
        endpoint: int,
        kind: MessageKind,
    ) -> ActionView:
        """A transaction: CORE assembles the request and the reply comes back the same way."""
        future: Future[Outcome] = Future()

        def task() -> None:
            if not future.set_running_or_notify_cancel():
                return
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

        self.core.submit(task)
        return _outcome(self._collect(future))

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
        data=item.data.hex(),
    )


def _plain(value: Value | None) -> Plain:
    if value is None or value is ABSENT:
        return None
    return value if isinstance(value, bool | int) else None


def _arg_type(field: Field, quantity: Quantity | None) -> str:
    try:
        return field_type(field, quantity)
    except ValueError:
        return field.type


def _quantity(model: EndpointModel) -> Quantity | None:
    """The quantity `Sense` measures — without it `Value` cannot be decoded."""
    state = model.attributes.get(sns.QUANTITY)
    if state is None or not isinstance(state.value, int) or isinstance(state.value, bool):
        return None
    try:
        return Quantity(state.value)
    except ValueError:
        return None


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
