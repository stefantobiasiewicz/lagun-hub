"""The views of the hub API — what the operations return.

Frozen dataclasses of plain values, ready to go through `json.dumps` without a translator.
**This module depends on nothing in `core/`**, and a test watches that: a client of a
running hub (the full-screen view) needs these shapes to assemble answers, and
must be able to have them without pulling in the CORE thread, the configuration store and
the binding engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

Plain = int | bool | str | None
"""What may be exposed in a view. As much as passes through JSON without a
translator."""


@dataclass(frozen=True, slots=True)
class AttributeView:
    id: int
    name: str
    type: str
    access: str
    value: Plain = None
    label: str | None = None
    """The value’s name, where it has one: `LIGHT` instead of `1`."""

    raw: str = ""
    age: float | None = None
    """Seconds since the last read. `None` means the hub has not seen this value."""

    wanted: Plain = None
    wanted_label: str | None = None
    matches: bool | None = None
    """`None` means "there is nothing to compare against", not "it agrees"."""

    choices: tuple[str, ...] = ()
    configurable: bool = False
    """`RW` and persistent — that is, configuration, not state and not a working setting
    (KONFIGURACJA.md §7)."""
    available: bool = True
    """`False` when this endpoint cannot have the attribute — its feature is not
    declared, or the node said it does not expose it and sets the value itself. Shown
    anyway, for the same reason as `CommandView.available`."""


@dataclass(frozen=True, slots=True)
class ArgView:
    """One command argument. Without this there is no sensible way to ask for it."""

    name: str
    type: str
    choices: tuple[str, ...] = ()
    lo: int | None = None
    hi: int | None = None


@dataclass(frozen=True, slots=True)
class CommandView:
    name: str
    args: tuple[ArgView, ...] = ()
    available: bool = True
    """`False` when the endpoint lacks the feature this command needs — we show it anyway,
    because "there is none" and "not here" are two different answers."""


@dataclass(frozen=True, slots=True)
class EndpointView:
    number: int
    type_id: int | None = None
    type_name: str = ""
    revision: int = 0
    features: int = 0
    channel: int = 0
    name: str = ""
    groups: tuple[int, ...] = ()
    quantity: str | None = None
    attributes: tuple[AttributeView, ...] = ()
    commands: tuple[CommandView, ...] = ()
    events: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NodeSummary:
    """As much as a list needs. The full view is dearer and not everybody needs it."""

    uid: str
    alias: str = ""
    name: str = ""
    room: str = ""
    address: int = 0
    bus: str = ""
    state: str = "UNKNOWN"
    endpoints: int = 0
    silent_for: float | None = None
    configured: bool = False
    """Whether this node is in the configuration file. A node that joined and is not
    configured is a normal thing — a freshly admitted one."""

    blocked: bool = False
    """The file says not to serve it (06-cykl-zycia-urzadzenia.md §8). Separate from
    `state`, because the file may say so about a node the hub has not seen yet."""


@dataclass(frozen=True, slots=True)
class NodeView(NodeSummary):
    short_id: int = 0
    vendor_id: int = 0
    device_type: int = 0
    firmware: int = 0
    detail: tuple[EndpointView, ...] = ()
    reading: int | None = None
    """Reads still ahead of `read_all` — `None` when the node is not being read. The view
    polls rather than listens, so this is how it shows progress (zadania/zrobione/FEAT-06.md,
    R10)."""


@dataclass(frozen=True, slots=True)
class TargetView:
    kind: str
    endpoint: int
    element: int
    uid: str = ""
    alias: str = ""
    group: int | None = None
    label: str = ""
    active: bool = False
    """Whether the target has an address today. A binding to an addressless node **does
    nothing** — and it is better for that to be visible than to be inferred from it not
    working."""


@dataclass(frozen=True, slots=True)
class BindingView:
    source_uid: str
    source_alias: str = ""
    source_endpoint: int = 0
    source_element: int = 0
    on_report: bool = False
    label: str = ""
    targets: tuple[TargetView, ...] = ()


@dataclass(frozen=True, slots=True)
class JoinView:
    short_id: int
    node_class: str
    address: int = 0
    bus: int = 0
    state: str = "UNKNOWN"
    uid: str | None = None
    last_seen: float | None = None


@dataclass(frozen=True, slots=True)
class CommissioningView:
    accepting: bool
    known: tuple[JoinView, ...] = ()


@dataclass(frozen=True, slots=True)
class BusView:
    index: int
    name: str


@dataclass(frozen=True, slots=True)
class HealthView:
    nodes: int
    ready: int
    configured: int
    issues: int
    buses: tuple[BusView, ...] = ()
    latency: Mapping[str, float] = field(default_factory=dict)
    bindings: Mapping[str, int] = field(default_factory=dict)
    core: Mapping[str, int] = field(default_factory=dict)
    transactions: Mapping[str, int] = field(default_factory=dict)
    reconcile: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HubInfo:
    """What one is talking to. The first thing every client asks."""

    name: str
    version: str
    nodes: int = 0
    ready: int = 0
    issues: int = 0
    buses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfigView:
    """The configuration as it stands in the files, together with a version for
    check-and-set."""

    etag: str
    files: tuple[str, ...] = ()
    document: Any = None


@dataclass(frozen=True, slots=True)
class DiffRow:
    """One piece of drift between the file and a node.

    The row that made writing the API early worth it: drift between the configuration
    and reality is the hardest thing to diagnose in an installation like this, and it is
    to be visible outright ([`ARCHITEKTURA.md`](../ARCHITEKTURA.md) §10.6).
    """

    uid: str
    alias: str
    endpoint: int
    attribute: str
    state: str
    """`match`, `mismatch`, `unknown` (the hub did not read it) or `missing` (there is no
    endpoint)."""

    wanted: Plain = None
    wanted_label: str | None = None
    actual: Plain = None
    actual_label: str | None = None


@dataclass(frozen=True, slots=True)
class EventView:
    """What happened in the installation. The same shape for SSE and for MQTT."""

    kind: str
    at: float
    uid: str = ""
    endpoint: int = 0
    element: int = 0
    name: str = ""
    value: Plain = None
    raw: str = ""
    text: str = ""


@dataclass(frozen=True, slots=True)
class RemoveView:
    """How removing a node ended (06-cykl-zycia-urzadzenia.md §6). A failed ordinary
    removal is an error, not this view — nothing was removed then."""

    uid: str
    acknowledged: bool | None
    """`True` — the node confirmed its reset and let go of its address. `None` — forced,
    the hub did not wait: the node may still be working under its old address and come
    back as `PENDING`."""

    address: int = 0
    """The address it had; `0` for a node the hub never saw."""

    unconfigured: bool = False
    """Its entry left the configuration file."""

    bindings: int = 0
    """How many bindings left the file with it."""


@dataclass(frozen=True, slots=True)
class EnumerateView:
    """A re-enumeration was started. How it ends arrives as an event — reading a node is
    a few dozen transactions, longer than a request should wait."""

    started: bool
    running: int = 0


@dataclass(frozen=True, slots=True)
class ReadoutView:
    """Reading everything was started. Like enumeration it returns at once: a relay with
    four channels is about 170 reads, longer than a request should wait. The values
    reach the node view as they come, and the end arrives as a `READOUT_DONE` event."""

    started: bool
    pending: int = 0
    """How many reads were queued."""

    running: int = 0
    """How many nodes are being read now, this one included."""


@dataclass(frozen=True, slots=True)
class ReconcileView:
    """How a reconcile request ended."""

    started: bool
    """`False` means "there was nothing to reconcile, or it is already running" — not an
    error."""

    pending: int = 0
    running: int = 0


@dataclass(frozen=True, slots=True)
class TrafficView:
    """One frame from the bus. The same thing an analyser shows, only with names."""

    at: float
    bus: str
    kind: str
    """A name from the registry: `COMMAND`, `RESPONSE`, `REPORT`. Whoever draws it
    translates."""

    outgoing: bool
    address: int = 0
    endpoint: int = 0
    element: int = 0
    uid: str = ""
    name: str = ""
    detail: str = ""
    tid: int | None = None
    result: str | None = None
    elapsed: float | None = None
    data: str = ""


@dataclass(frozen=True, slots=True)
class ActionView:
    """How a command, a write or a read ended."""

    ok: bool
    result: str | None
    attempts: int
    elapsed: float
    data: str = ""


__all__ = [
    "ActionView",
    "ArgView",
    "AttributeView",
    "BindingView",
    "BusView",
    "CommandView",
    "CommissioningView",
    "ConfigView",
    "DiffRow",
    "EndpointView",
    "EnumerateView",
    "EventView",
    "HealthView",
    "HubInfo",
    "JoinView",
    "NodeSummary",
    "NodeView",
    "Plain",
    "ReadoutView",
    "ReconcileView",
    "RemoveView",
    "TargetView",
    "TrafficView",
]
