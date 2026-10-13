"""The installation model and the immutable image for the fast path.

Two things in one file, because one follows from the other:

* **`Installation`** — the mutable model, owned solely by the CORE thread,
* **`Snapshot`** — what CORE derives from it for the fast path.

Everything is keyed by `Uid`, never by address. The address appears in exactly two
places — `Installation.by_address` and `Snapshot.addr` — and those are the only two
that have to be rebuilt after readdressing
([`07-hub.md`](../lagunbus-spec/07-hub.md) §2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import NewType, Protocol

from lagunhub.protocol.endpoints import CATALOG
from lagunhub.protocol.endpoints.definition import QUANTITY_TYPE, AttributeDef, TypeDef
from lagunhub.protocol.frame import encode_header, encode_id
from lagunhub.protocol.quantities import Quantity, raw_type
from lagunhub.protocol.registry import NODE_CLASS_BLOCKS, EndpointType, MessageKind, NodeClass
from lagunhub.protocol.values import Value, decode

Uid = NewType("Uid", bytes)
"""A node’s identity. An address is a route and changes; a Uid never does."""

Source = tuple[Uid, int, int]
"""``(Uid, endpoint, element)`` — the triple a binding is matched by."""


class EndpointWanted(Protocol):
    """What the configuration wants from an endpoint. A shape, not an import — the model
    knows nothing of the file format ([`config/`](config/))."""

    @property
    def type(self) -> EndpointType | None:
        """The type the operator expects — `expect` from KONFIGURACJA.md §10."""
        ...

    @property
    def groups(self) -> tuple[int, ...]: ...

    @property
    def attributes(self) -> Mapping[int, Value]: ...


class NodeWanted(Protocol):
    @property
    def alias(self) -> str:
        """The name a person gave it, the one bindings refer to."""
        ...

    @property
    def name(self) -> str: ...

    @property
    def room(self) -> str: ...

    @property
    def endpoints(self) -> Mapping[int, EndpointWanted]: ...


class NodeState(Enum):
    """The state the hub keeps for every `ShortId` it has ever seen
    (06-cykl-zycia-urzadzenia.md §1.2).

    There is no state for a removed node, and there used to be one — `RETIRED`. It kept
    a note ("this `ShortId` may speak up again") that nothing acted on: a retired record
    was treated exactly like a missing one. Removing now forgets the record, and the
    state that stays is `BLOCKED` — a node the hub knows and deliberately does not serve
    (lagunbus-spec/DECYZJE.md, "Usunięcie: zapomnienie w dwóch trybach; zablokowanie
    osobno").
    """

    UNKNOWN = auto()
    PENDING = auto()
    JOINED = auto()
    READY = auto()
    ABSENT = auto()
    BLOCKED = auto()


@dataclass(slots=True)
class JoinRecord:
    """Join state, kept **for every `ShortId` the hub has seen**
    (06-cykl-zycia-urzadzenia.md §1.2).

    This is a table separate from the installation model, and it has to be: between
    assigning an address and enumerating, the hub knows the `ShortId` and the address
    but **does not yet know the `Uid`** — and the `Uid` is the model’s key. Only
    reading the identity joins the two.
    """

    short_id: int
    node_class: NodeClass = NodeClass.UNSPECIFIED
    address: int = 0
    bus: int = 0
    state: NodeState = NodeState.UNKNOWN
    uid: Uid | None = None
    last_seen: float = 0.0


class AddressPool:
    """The address pool, split into blocks by node class (03-adresacja.md §2.3).

    The block is chosen **once, while joining**, and the node stays in it for life.
    At that moment the hub has no way to read anything from the node, because reading
    needs an address — so the `NodeClass` from the announcement decides, one field
    nobody has to interpret.
    """

    def __init__(self) -> None:
        self._taken: set[int] = set()
        self._heard: set[int] = set()
        """Addresses a node has spoken from since the hub started
        ([`03-adresacja.md`](../lagunbus-spec/03-adresacja.md) §6).

        A separate set, not `_taken`, because it answers a different question: `_taken`
        is what the database says, `_heard` is what the bus says — and the two part ways
        exactly when it matters: a database restored from a backup older than some node's
        joining, or a removed node that never got its `FactoryReset`. Kept in memory only:
        after a restart every working node is heard again within one reporting period,
        and an address of a node removed for good stays blocked until then — out of two
        thousand, no cost at all."""

    def reserve(self, address: int) -> None:
        if address:
            self._taken.add(address)

    def release(self, address: int) -> None:
        self._taken.discard(address)

    def hear(self, address: int) -> None:
        self._heard.add(address)

    def unhear(self, address: int) -> None:
        self._heard.discard(address)

    def taken(self, address: int) -> bool:
        return address in self._taken or address in self._heard

    def allocate(self, node_class: NodeClass) -> int | None:
        """The first free one from the block the class points at. `None` when it is full.

        Free means neither in the database nor heard on the bus."""
        lo, hi = NODE_CLASS_BLOCKS[node_class]
        for address in range(lo, hi + 1):
            if not self.taken(address):
                self._taken.add(address)
                return address
        return None

    def __len__(self) -> int:
        return len(self._taken)


# --- the mutable model, for CORE only --------------------------------------------


@dataclass(slots=True)
class AttributeState:
    """The last known value of an attribute.

    ``raw`` is always there, ``value`` only once the hub knows the endpoint type.
    Before enumeration it rarely does — and it is better to keep bytes we cannot
    interpret than to pretend they never arrived.
    """

    raw: bytes
    value: Value | None
    at: float


@dataclass(slots=True)
class EndpointModel:
    number: int
    type_id: EndpointType | None = None
    revision: int = 0
    features: int = 0
    channel: int = 0
    attributes: dict[int, AttributeState] = field(default_factory=dict)
    absent: set[int] = field(default_factory=set)
    """Optional attributes this node answered `UNSUPPORTED_ELEMENT` for.

    The descriptor shows features, not which optional attributes an endpoint carries —
    a key with `HOLD` may expose `HoldThreshold` or set the threshold itself
    (types/0x0010-key-input.md §2.4). The hub learns it from the first refusal while
    reconciling rather than by probing every optional attribute at enumeration: that
    would cost up to a dozen transactions per key to answer a question nobody has asked
    yet (zadania/zrobione/CR-01.md, N10).

    Not stored: after a restart the hub learns it again at the first reconciliation,
    and a new enumeration builds a new model, so a firmware that starts exposing the
    attribute is not held back by old knowledge.
    """

    @property
    def definition(self) -> TypeDef | None:
        return CATALOG.get(self.type_id) if self.type_id is not None else None

    def lacks(self, meta: AttributeDef, value: Value | None = None) -> str | None:
        """Why this endpoint cannot take `meta` (with `value`, when given) — or `None`.

        One answer for the API and for reconciling alike: the hub configures only what
        the endpoint declared (01-model-danych.md §2.1, COMPLIANCE `LB-HUB-026`).
        """
        if meta.feature is not None and not self.features & meta.feature:
            return f"{meta.name} needs a feature this endpoint does not declare"
        if meta.id in self.absent:
            return f"the device does not expose {meta.name} — it sets it itself"
        needed = meta.value_feature(value) if value is not None else None
        if needed is not None and not self.features & needed:
            return f"{meta.name} = {value} needs a feature this endpoint does not declare"
        return None


@dataclass(slots=True)
class NodeModel:
    uid: Uid
    address: int
    bus: int
    state: NodeState = NodeState.UNKNOWN
    short_id: int = 0
    vendor_id: int = 0
    device_type: int = 0
    firmware: int = 0
    name: str = ""
    endpoints: dict[int, EndpointModel] = field(default_factory=dict)
    last_heard: float = 0.0
    silence_threshold: float = 0.0
    described: bool = True
    """Whether the hub has read the descriptor of the node it holds.

    `False` only for a node taken from the file after a restart: the address is known,
    so commands and bindings work, but the endpoints are not — the file keeps where a
    node is, not what it is (zadania/zrobione/BUG-05.md, N9). The hub reads it at the node's
    first frame, or first, before an operation that needs it — never by polling
    everybody at start (07-hub.md §5.1)."""

    def endpoint(self, number: int) -> EndpointModel:
        """An endpoint the hub knows nothing about yet is still an endpoint."""
        existing = self.endpoints.get(number)
        if existing is None:
            existing = EndpointModel(number=number)
            self.endpoints[number] = existing
        return existing


@dataclass(frozen=True, slots=True)
class BindingSpec:
    """A binding that is **independent of addresses**.

    The target frame is assembled only while building the snapshot, from current
    addresses. Otherwise readdressing a node would leave a precompiled frame in memory
    pointing at somebody else’s address — a fault visible only once the wrong light
    comes on.
    """

    source: Source
    target_endpoint: int
    target_element: int
    target_uid: Uid | None = None
    group: int | None = None
    args: bytes = b""
    kind: MessageKind = MessageKind.COMMAND
    on_report: bool = False
    """The source is a change of a reported attribute, not an event.

    Such a binding is **not a fast path**: a report travels at priority `0x8` and has
    no time budget, and it fires on a **change of value**, not on every report
    ([`07-hub.md`](../lagunbus-spec/07-hub.md) §3.1). A periodic refresh carrying the
    same value as before is not an event and starts nothing.
    """

    label: str = ""
    """A description of **the target**: "kitchen-relay ep4 Output.Toggle"."""

    source_label: str = ""
    """A description of the **source**. A field of its own, because one binding has two
    ends and both have to be nameable — otherwise the bindings view shows the target
    on both sides of the arrow."""


@dataclass(slots=True)
class Installation:
    """Everything the hub knows about the installation. Owned solely by CORE."""

    nodes: dict[Uid, NodeModel] = field(default_factory=dict)
    by_address: dict[int, Uid] = field(default_factory=dict)
    bindings: list[BindingSpec] = field(default_factory=list)
    records: dict[int, JoinRecord] = field(default_factory=dict)
    """Join state per `ShortId` — including nodes whose `Uid` the hub does not know."""

    addresses: AddressPool = field(default_factory=AddressPool)

    wanted: dict[Uid, NodeWanted] = field(default_factory=dict)
    """Configuration per node — including ones the hub has not seen yet."""

    blocked: frozenset[Uid] = frozenset()
    """Nodes the operator told the hub not to serve — from the configuration file,
    keyed by `Uid` (06-cykl-zycia-urzadzenia.md §8).

    The file, keyed by `Uid`: a block is an operator's decision, it has to survive a
    restart, and the `Uid` — unlike the address — never changes.
    """

    issues: list[str] = field(default_factory=list)
    """Things needing the operator’s attention. The hub does not guess at them, it shows
    them
    ([`07-hub.md`](../lagunbus-spec/07-hub.md) §5.3)."""

    # --- model changes --------------------------------------------------------

    def add(self, node: NodeModel) -> NodeModel:
        self.nodes[node.uid] = node
        if node.address:
            self.by_address[node.address] = node.uid
            self.addresses.reserve(node.address)
        return node

    def is_blocked(self, uid: Uid | None) -> bool:
        return uid is not None and uid in self.blocked

    def forget(self, uid: Uid) -> NodeModel | None:
        """Remove a node **without a trace** — the model, its address and every join record
        that names it (06-cykl-zycia-urzadzenia.md §6).

        The address goes back to the pool but stays *heard*: whether the node really let
        go of it is for the caller to say, once the node acknowledged its reset.
        """
        node = self.nodes.pop(uid, None)
        if node is not None and node.address:
            if self.by_address.get(node.address) == uid:
                del self.by_address[node.address]
            self.addresses.release(node.address)
        for short_id, record in list(self.records.items()):
            if record.uid == uid or (node is not None and short_id == node.short_id):
                if record.address and record.address not in self.by_address:
                    self.addresses.release(record.address)
                del self.records[short_id]
        return node

    def record(self, short_id: int) -> JoinRecord:
        """A join record. An unknown `ShortId` gets one too — in state `UNKNOWN`."""
        existing = self.records.get(short_id)
        if existing is None:
            existing = JoinRecord(short_id=short_id)
            self.records[short_id] = existing
        return existing

    def record_at(self, bus: int, address: int) -> JoinRecord | None:
        """The record holding this address **on this bus**.

        An address is unique across the whole installation
        ([`03-adresacja.md`](../lagunbus-spec/03-adresacja.md) §2.1), so the address
        alone would do — as long as the contract holds. The bus number is here exactly
        to catch the moment it does not: without it a frame from the other bus lands
        on somebody else's record and the hub **masks** a conflict only it can see
        (03-adresacja.md §6).
        """
        for record in self.records.values():
            if record.address == address and record.bus == bus:
                return record
        return None

    def holder(self, address: int) -> JoinRecord | Uid | None:
        """Whoever holds this address in the hub's database, on **any** bus — or `None`
        when the address is a stranger's (06-cykl-zycia-urzadzenia.md §2.3).

        The bus is deliberately ignored: an address known on another bus is not a
        stranger but a conflict, and `foreign_record_at` handles that.
        """
        uid = self.by_address.get(address)
        if uid is not None:
            return uid
        for record in self.records.values():
            if record.address == address:
                return record
        return None

    def foreign_record_at(self, bus: int, address: int) -> JoinRecord | None:
        """The record holding this address on **another** bus — that is, a conflict
        (03-adresacja.md §6).

        Nodes on separate buses never hear each other, so only the hub sees this case.
        """
        for record in self.records.values():
            if record.address == address and record.bus != bus and record.short_id:
                return record
        return None

    def readdress(self, uid: Uid, address: int) -> None:
        """The only place where a node’s address changes.

        Whoever calls it writes the new address to the file as well
        (`CoreThread.note_joined`) — the file is where the address survives a restart."""
        node = self.nodes[uid]
        self.by_address.pop(node.address, None)
        self.addresses.release(node.address)
        node.address = address
        if address:
            self.by_address[address] = uid
            self.addresses.reserve(address)

    def heard(self, bus: int, address: int, now: float) -> NodeModel | None:
        """Every frame from a node moves `last_heard` forward — of any kind
        (06-cykl-zycia-urzadzenia.md §5)."""
        record = self.record_at(bus, address)
        if record is not None:
            record.last_seen = now
        uid = self.by_address.get(address)
        if uid is None:
            return None
        node = self.nodes[uid]
        node.last_heard = now
        return node

    def remember(
        self, bus: int, address: int, endpoint: int, attribute: int, raw: bytes, now: float
    ) -> bool:
        """Remember an attribute value. Returns whether it **changed**.

        One route for both sources: a report that arrived on its own, and a reply to a
        read the hub asked for. Before the hub API there was only the first — so the hub
        read `Purpose` while reconciling, compared it and **threw the read value away**.
        The model knew the installation’s structure and not a single setting the node
        does not report by itself.
        """
        node = self.heard(bus, address, now)
        if node is None:
            return False
        target = node.endpoint(endpoint)
        prior = target.attributes.get(attribute)
        target.attributes[attribute] = AttributeState(
            raw=raw, value=_decode(target, attribute, raw), at=now
        )
        return prior is None or prior.raw != raw

    def on_report(
        self, bus: int, address: int, endpoint: int, attribute: int, raw: bytes, now: float
    ) -> bool:
        """A report restores an attribute's state **without polling**
        (01-model-danych.md §6).

        Returns whether the value changed — because a binding on an attribute fires on
        a change, not on every report. Otherwise a periodic refresh would generate
        commands in circles (07-hub.md §3.1).
        """
        return self.remember(bus, address, endpoint, attribute, raw, now)

    # --- the image for the fast path ------------------------------------------

    def group_buses(self, group: int) -> tuple[int, ...]:
        """The buses the members of a group sit on.

        A group spread across three buses is three frames — but still one per line, so
        the saving stays where it matters (07-hub.md §6).
        """
        buses = {
            node.bus
            for uid, node in self.nodes.items()
            if node.address and group in self.groups_of(uid)
        }
        return tuple(sorted(buses))

    def groups_of(self, uid: Uid) -> tuple[int, ...]:
        wanted = self.wanted.get(uid)
        if wanted is None:
            return ()
        return tuple({group for endpoint in wanted.endpoints.values() for group in endpoint.groups})

    def snapshot(self) -> Snapshot:
        """Build a new immutable image. Only CORE calls this."""
        bind: dict[Source, tuple[Target, ...]] = {}
        on_report: dict[Source, tuple[Target, ...]] = {}
        for spec in self.bindings:
            if self.is_blocked(spec.source[0]) or self.is_blocked(spec.target_uid):
                # A blocked node triggers nothing and is the target of nothing
                # (06-cykl-zycia-urzadzenia.md §8). Dropped here, from the image, so the
                # fast path needs no check of its own — it never sees the binding.
                continue
            targets = self._compile(spec)
            if not targets:
                continue
            where = on_report if spec.on_report else bind
            where[spec.source] = (*where.get(spec.source, ()), *targets)
        return Snapshot(
            addr=dict(self.by_address),
            bind=bind,
            on_report=on_report,
            names=self._names(),
        )

    def _names(self) -> dict[Uid, str]:
        names = {uid: node.name for uid, node in self.nodes.items() if node.name}
        names.update({uid: w.name for uid, w in self.wanted.items() if w.name})
        return names

    def _compile(self, spec: BindingSpec) -> tuple[Target, ...]:
        """One shape of code for three shapes of target — the hub need not tell them apart."""
        if spec.kind == MessageKind.GROUP_COMMAND and spec.group is not None:
            return tuple(
                Target(
                    bus=bus,
                    can_id=encode_id(
                        MessageKind.GROUP_COMMAND,
                        spec.group,
                        spec.target_endpoint,
                        spec.target_element,
                    ),
                    payload=spec.args,  # a group command has no Tid and no reply
                    transactional=False,
                    label=spec.label,
                )
                for bus in self.group_buses(spec.group)
            )

        if spec.target_uid is None:
            return ()
        node = self.nodes.get(spec.target_uid)
        if node is None or not node.address:
            return ()  # a target without an address — the binding simply does not work
        return (
            Target(
                bus=node.bus,
                can_id=encode_id(
                    spec.kind, node.address, spec.target_endpoint, spec.target_element
                ),
                payload=bytes([encode_header(0)]) + spec.args,
                transactional=True,
                label=spec.label,
            ),
        )


def _decode(endpoint: EndpointModel, attribute: int, raw: bytes) -> Value | None:
    definition = endpoint.definition
    if definition is None:
        return None
    meta = definition.attributes.get(attribute)
    if meta is None or meta.type in ("string", "blob"):
        return None  # not a `Value`; the API decodes a string from `raw` for the view
    type_name = meta.type if meta.type != QUANTITY_TYPE else _quantity_type(endpoint)
    if type_name is None:
        return None  # `Quantity` not known yet — the bytes are kept, the number is not
    try:
        return decode(type_name, raw)
    except ValueError:
        return None


def _quantity_type(endpoint: EndpointModel) -> str | None:
    """The wire type of `<Q>` on this endpoint, from its `Quantity` as the hub last saw
    it — `Sense` and `Display` alike (zadania/zrobione/FEAT-12.md). `Quantity` is the device's and
    never changes in operation (types/0x0030-sense.md §2.3, types/0x0022-display.md §5.2),
    so a value decoded once stays right."""
    definition = endpoint.definition
    if definition is None:
        return None
    try:
        number = definition.attribute("Quantity").id
    except ValueError:
        return None
    state = endpoint.attributes.get(number)
    if state is None or not isinstance(state.value, int) or isinstance(state.value, bool):
        return None
    try:
        return raw_type(Quantity(state.value))
    except ValueError:
        return None


# --- the immutable image for the fast path ---------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """A binding target — **a finished frame**, not a description of what to build.

    The only variable part is the ``Tid``: three bits in byte ``[0]``.
    """

    bus: int
    can_id: int
    payload: bytes
    transactional: bool
    label: str = ""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Nothing but what the fast path needs.

    The swap is wholesale: CORE builds a new object and substitutes the reference,
    while RX reads it once per frame into a local. There is no intermediate state and
    no lock.
    """

    addr: dict[int, Uid] = field(default_factory=dict)
    bind: dict[Source, tuple[Target, ...]] = field(default_factory=dict)
    on_report: dict[Source, tuple[Target, ...]] = field(default_factory=dict)
    """Bindings on attribute change. CORE reads them, not the fast path."""

    inputs: dict[Source, bytes] = field(default_factory=dict)
    names: dict[Uid, str] = field(default_factory=dict)
