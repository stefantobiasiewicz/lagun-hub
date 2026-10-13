"""Identifying a device — 06-cykl-zycia-urzadzenia.md §3.

The hub reads the **identity** and the **descriptor** from the node, and then asks
nothing further: the read from the node is decisive and is the only source. The model
registry holds no endpoint lists and never will — a device may have more endpoints
after a firmware update than it had when it was made
([`types/0x0001-device.md`](../lagunbus-spec/types/0x0001-device.md) §5.2).

Enumeration is a **run of transactions**, not one: four reads per endpoint plus a few
per node. A device with eight endpoints is about thirty reads, once — of no
consequence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from lagunhub.core.model import (
    AttributeState,
    EndpointModel,
    Installation,
    JoinRecord,
    NodeModel,
    NodeState,
    Uid,
)
from lagunhub.core.transactions import Outcome
from lagunhub.protocol.endpoints import CATALOG
from lagunhub.protocol.endpoints import device as dev
from lagunhub.protocol.registry import (
    FIRST_FUNCTIONAL_ENDPOINT,
    SYSTEM_ENDPOINTS,
    CommonAttribute,
    EndpointType,
    ResultCode,
)
from lagunhub.protocol.values import SCALARS, decode, decode_blob, encode

SCENES_ENDPOINT: Final = 3
"""The only system endpoint a node may lack (01-model-danych.md §1.2)."""

Request = Callable[[int, int, int, int, Callable[[Outcome], None]], None]
"""``(bus, address, endpoint, element, done)``."""

DEVICE_READS: Final[tuple[int, ...]] = (
    dev.UID,
    dev.DEVICE_TYPE,
    dev.VENDOR_ID,
    dev.PROTOCOL_VERSION,
    dev.FIRMWARE_VERSION,
    dev.HARDWARE_VERSION,
    dev.ENDPOINT_COUNT,
)
"""06-cykl-zycia-urzadzenia.md §3.1 plus `VendorId`, because a model’s identity is the
**pair** with `DeviceType`."""

ENDPOINT_READS: Final[tuple[int, ...]] = (
    CommonAttribute.TypeId,
    CommonAttribute.TypeRevision,
    CommonAttribute.Features,
    CommonAttribute.Channel,
)
"""We always read `Channel`, single-channel devices included: the hub does not know in
advance whether the relation will occur, and a conditional read would save one
transaction at the cost of a branch in a procedure that runs once in a device’s life
(06-cykl-zycia-urzadzenia.md §3.1)."""


DECLARATIONS: Final[tuple[str, ...]] = ("Quantity", "TextLength")
"""06-cykl-zycia-urzadzenia.md §3.1, the declarations. Attributes of a type that
**describe** an endpoint rather than its state: fixed by
the device, read-only, never changing in operation. Read with the descriptor once the
endpoint's type is known.

`Quantity` because without it `<Q>` cannot be decoded or encoded at all — a `Sense`
value or a `Display` write (types/0x0030-sense.md §2.3, types/0x0022-display.md §5.2).
`TextLength` so that the hub refuses a text the field cannot hold before sending it
(§5.3). One read each, once in a device's life; a field without the feature answers
`UNSUPPORTED_FEATURE`, which is a normal answer here (zadania/zrobione/FEAT-12.md)."""


@dataclass
class Job:
    """One identification in progress."""

    short_id: int
    address: int
    bus: int
    uid: Uid | None = None
    device: dict[int, int] = field(default_factory=dict)
    endpoints: dict[int, dict[int, int]] = field(default_factory=dict)
    pending: list[tuple[int, int]] = field(default_factory=list)
    expanded: bool = False
    then: list[Callable[[bool], None]] = field(default_factory=list)
    """Who waits for this enumeration to end — `True` when the model is in place."""


class Enumerator:
    """Runs identifications. Belongs solely to the CORE thread."""

    def __init__(
        self,
        installation: Installation,
        request: Request,
        on_ready: Callable[[NodeModel], None] | None = None,
        on_conflict: Callable[[int, Uid, int], None] | None = None,
        on_failed: Callable[[JoinRecord, str], None] | None = None,
        on_start: Callable[[JoinRecord], None] | None = None,
    ) -> None:
        self.installation = installation
        self._request = request
        self._on_ready = on_ready
        self._on_conflict = on_conflict
        self._on_failed = on_failed
        self._on_start = on_start
        self._jobs: dict[int, Job] = {}

        self.started = 0
        self.completed = 0
        self.failed = 0

    def __len__(self) -> int:
        return len(self._jobs)

    def busy(self, short_id: int) -> bool:
        return short_id in self._jobs

    # --- the beginning --------------------------------------------------------

    def start(self, record: JoinRecord, then: Callable[[bool], None] | None = None) -> bool:
        """Start reading the node. `then` is told how it ended — also when an enumeration
        of this node is already running, which it then waits for rather than starting a
        second. Returns whether this call started one."""
        if not record.address:
            if then is not None:
                then(False)
            return False
        running = self._jobs.get(record.short_id)
        if running is not None:
            if then is not None:
                running.then.append(then)
            return False
        job = Job(short_id=record.short_id, address=record.address, bus=record.bus)
        if then is not None:
            job.then.append(then)
        job.pending = [(0, element) for element in DEVICE_READS]
        self._jobs[record.short_id] = job
        self.started += 1
        if self._on_start is not None:
            self._on_start(record)
        self._step(job)
        return True

    # --- step by step ---------------------------------------------------------

    def _step(self, job: Job) -> None:
        if job.pending:
            endpoint, element = job.pending.pop(0)
            self._request(
                job.bus,
                job.address,
                endpoint,
                element,
                lambda outcome: self._got(job, endpoint, element, outcome),
            )
            return
        if not job.expanded:
            self._expand(job)
            self._step(job)
            return
        self._finish(job)

    def _got(self, job: Job, endpoint: int, element: int, outcome: Outcome) -> None:
        if not outcome.answered:
            self._abandon(job, "the node does not answer")
            return
        if outcome.result == ResultCode.OK:
            self._store(job, endpoint, element, outcome.data)
        elif outcome.result not in (
            ResultCode.UNSUPPORTED_ELEMENT,
            ResultCode.UNSUPPORTED_FEATURE,
        ):
            # An optional attribute the node lacks is a normal state. Any other refusal
            # means the descriptor cannot be read.
            self._abandon(job, f"read 0x{element:02X} → {outcome.result}")
            return
        self._step(job)

    def _store(self, job: Job, endpoint: int, element: int, data: bytes) -> None:
        if endpoint == 0 and element == dev.UID:
            job.uid = Uid(decode_blob(data))
            return
        value = decode(_width(len(data)), data)
        count = int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
        if endpoint == 0:
            job.device[element] = count
            return
        job.endpoints.setdefault(endpoint, {})[element] = count
        if element == CommonAttribute.TypeId:
            job.pending.extend((endpoint, number) for number in _declarations(count))

    def _expand(self, job: Job) -> None:
        """Functional endpoints are numbered from 4 **with no gaps**
        (06-cykl-zycia-urzadzenia.md §3.2).

        Without that rule the hub would have to poll all sixty possible numbers and
        recognise the absent ones by the `UNSUPPORTED_ENDPOINT` code.
        """
        job.expanded = True
        count = job.device.get(dev.ENDPOINT_COUNT, 0)
        job.pending = [
            (endpoint, element)
            for endpoint in range(FIRST_FUNCTIONAL_ENDPOINT, FIRST_FUNCTIONAL_ENDPOINT + count)
            for element in ENDPOINT_READS
        ]

    # --- the end --------------------------------------------------------------

    def _finish(self, job: Job) -> None:
        del self._jobs[job.short_id]
        if job.uid is None:
            self.failed += 1
            _tell(job, False)
            return

        previous = self.installation.nodes.get(job.uid)
        if previous is not None and previous.address != job.address and self._on_conflict:
            # The same Uid at two addresses — the hub cannot settle this by itself.
            #
            # What goes out is the OLD ADDRESS, not `previous.uid`: `previous` is looked
            # up BY
            # `job.uid`, so its `uid` is necessarily the same value and the report
            # carried the same Uid twice. The address this node has been answering from
            # until now is the only thing that makes this conflict diagnosable.
            self._on_conflict(job.address, job.uid, previous.address)

        node = NodeModel(
            uid=job.uid,
            address=job.address,
            bus=job.bus,
            state=NodeState.READY,
            short_id=job.short_id,
            vendor_id=job.device.get(dev.VENDOR_ID, 0),
            device_type=job.device.get(dev.DEVICE_TYPE, 0),
            firmware=job.device.get(dev.FIRMWARE_VERSION, 0),
            name=self._name(job.uid, previous),
            last_heard=self.installation.record(job.short_id).last_seen,
        )
        for number, reads in job.endpoints.items():
            node.endpoints[number] = EndpointModel(
                number=number,
                type_id=_endpoint_type(reads.get(CommonAttribute.TypeId)),
                revision=reads.get(CommonAttribute.TypeRevision, 0),
                features=reads.get(CommonAttribute.Features, 0),
                channel=reads.get(CommonAttribute.Channel, 0),
            )
            model = node.endpoints[number]
            for element in _declarations(reads.get(CommonAttribute.TypeId)):
                if element in reads:
                    meta = model.definition.attributes[element] if model.definition else None
                    type_name = meta.type if meta is not None else "u8"
                    model.attributes[element] = AttributeState(
                        raw=encode(type_name, reads[element]),
                        value=reads[element],
                        at=node.last_heard,
                    )
        self._system(node, job)
        self.installation.add(node)

        record = self.installation.record(job.short_id)
        record.uid = job.uid
        record.state = NodeState.READY
        if self.installation.is_blocked(job.uid):
            # Read, and kept — an unblock then has a model to start from — but not
            # served (06-cykl-zycia-urzadzenia.md §8). This is how a blocked node that
            # joined without an address is recognised: only now is its `Uid` known.
            node.state = record.state = NodeState.BLOCKED
        self.completed += 1
        if self._on_ready is not None:
            self._on_ready(node)
        _tell(job, True)

    def _system(self, node: NodeModel, job: Job) -> None:
        """System endpoints are **inserted, not polled** (01-model-danych.md §1.2).

        The specification fixes the numbers and types, so asking about them would be
        asking about something already known. Before the shell (an early client, since
        removed) the hub simply **did not have them in the model** — it knew the contents
        of `Device`, because it had read them, but had nowhere to put them. The effect only
        showed on an attempt to use it: `cmd <node> ep0 Reboot` answered "there is no
        ep0", while `ep1 ResetCounters` and all group handling on `ep2` were out of reach.

        `ep3` is the only conditional one, and its condition is derivable from the
        descriptor too: scenes exist if and only if the node has some `Output`.
        """
        when = node.last_heard
        for number, type_name in SYSTEM_ENDPOINTS.items():
            if number == SCENES_ENDPOINT and not scenes_present(node):
                continue
            node.endpoints[number] = EndpointModel(number=number, type_id=type_name)

        # What enumeration read from ep0 anyway now has somewhere to sit — and so it
        # shows in the node view instead of vanishing into header fields.
        definition = CATALOG[EndpointType.Device]
        for element, value in job.device.items():
            meta = definition.attributes.get(element)
            if meta is None or meta.type not in SCALARS:
                continue
            node.endpoints[0].attributes[element] = AttributeState(
                raw=encode(meta.type, value), value=value, at=when
            )

    def _name(self, uid: Uid, previous: NodeModel | None) -> str:
        """A person gives the name in the configuration; the node does not name itself
        (02-typy-danych.md §2.2)."""
        wanted = self.installation.wanted.get(uid)
        if wanted is not None and wanted.name:
            return wanted.name
        return previous.name if previous else ""

    def _abandon(self, job: Job, reason: str) -> None:
        """Give up. A model the hub already had **stays**: a re-enumeration the operator
        asked for, ending on a silent node, must not switch that node's bindings off
        (zadania/zrobione/FEAT-04.md, R9)."""
        self._jobs.pop(job.short_id, None)
        record = self.installation.record(job.short_id)
        record.state = NodeState.ABSENT
        self.failed += 1
        if self._on_failed is not None:
            self._on_failed(record, reason)
        _tell(job, False)


def _tell(job: Job, ready: bool) -> None:
    for then in job.then:
        then(ready)


def _declarations(type_id: int | None) -> tuple[int, ...]:
    """The `DECLARATIONS` this endpoint type has, by number."""
    endpoint_type = _endpoint_type(type_id)
    definition = CATALOG.get(endpoint_type) if endpoint_type is not None else None
    if definition is None:
        return ()
    return tuple(meta.id for meta in definition.attributes.values() if meta.name in DECLARATIONS)


def _width(length: int) -> str:
    return {1: "u8", 2: "u16", 4: "u32"}.get(length, "u8")


def _endpoint_type(value: int | None) -> EndpointType | None:
    if value is None:
        return None
    try:
        return EndpointType(value)
    except ValueError:
        # A vendor type, or one newer than this hub. The endpoint stays in the model as
        # unknown while the rest of the device works normally
        # (registry/endpoint-types).
        return None


def scenes_present(node: NodeModel) -> bool:
    """`ep3` exists if and only if the node has some `Output`
    (01-model-danych.md §1.2).

    The rule is **derivable from the descriptor**, so the system endpoints remain the
    thing one does not poll.
    """
    return any(endpoint.type_id == EndpointType.Output for endpoint in node.endpoints.values())
