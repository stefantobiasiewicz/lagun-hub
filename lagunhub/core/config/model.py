"""Hub configuration — what cannot be recovered from the bus.

The installation model is read from the nodes and rebuilds itself within one
`ReportInterval`. **Human decisions never rebuild**: names, bindings, the meaning of
groups, the sources of inputs ([`KONFIGURACJA.md`](../../KONFIGURACJA.md) §1).

There are no addresses here and there will not be. An address is a route, not an
identity — a configuration written "to address `0x2A`" falls apart at the first
renumbering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from lagunhub.core.model import Uid
from lagunhub.protocol.registry import EndpointType, MessageKind
from lagunhub.protocol.values import Value

DEFAULT_BITRATE: Final = 500_000
"""W2 settled in favour of the faster one. Throughput is not a factor
(05-mapowanie-can.md §7) — cable length decides, so the value is to be easy to
change."""


@dataclass(frozen=True, slots=True)
class BusConfig:
    name: str
    channel: str
    bitrate: int = DEFAULT_BITRATE

    @property
    def link_command(self) -> str:
        """The hub does not set the bitrate, the kernel does — and this is the command that
        does it."""
        return f"sudo ip link set {self.channel} type can bitrate {self.bitrate}"


@dataclass(frozen=True, slots=True)
class GroupConfig:
    """A group: a number and a name.

    The key is the **number**, because that is what travels in a frame’s `ADDRESS`
    field. The name is a label for a human — the same rule as with nodes, where the key
    is the Uid and the alias is a field
    ([`KONFIGURACJA.md`](../../../KONFIGURACJA.md) §4).
    """

    number: int
    name: str = ""


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    """What the operator decided about this endpoint.

    Attributes are **named, not numbered** — a number would be unreadable and could not
    be checked without hardware ([`KONFIGURACJA.md`](../../KONFIGURACJA.md) §7).
    """

    type: EndpointType | None = None
    name: str = ""
    attributes: dict[int, Value] = field(default_factory=dict)
    groups: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class NodeConfig:
    alias: str
    uid: Uid
    name: str = ""
    room: str = ""
    endpoints: dict[int, EndpointConfig] = field(default_factory=dict)
    blocked: bool = False
    """The hub knows this node and does not serve it (06-cykl-zycia-urzadzenia.md §8).

    A decision about the installation, so it lives here, next to the name — and not in
    the database, which does not survive a restart yet and would key it by an address
    that changes."""


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A binding’s source: an event, or a change of a reported attribute
    (07-hub.md §3.1)."""

    uid: Uid
    endpoint: int
    element: int
    on_report: bool = False
    label: str = ""


@dataclass(frozen=True, slots=True)
class TargetRef:
    """A binding’s target in three shapes, all served by the same engine."""

    kind: MessageKind
    uid: Uid | None = None
    group: int | None = None
    endpoint: int = 0
    element: int = 0
    args: bytes = b""
    label: str = ""


@dataclass(frozen=True, slots=True)
class BindingConfig:
    source: SourceRef
    targets: tuple[TargetRef, ...]


DEFAULT_API_PORT: Final = 8080
"""The hub server’s port. `zigbee2mqtt` sits on 8080 and there is no reason to be
original."""


@dataclass(frozen=True, slots=True)
class ApiConfig:
    """The hub server — the service channel the CLI, the front end and everything else
    drive.

    `host` defaults to the **loopback**, not `0.0.0.0`. The hub controls physical
    things, a gate and a lock among them, so exposing it to the network is to be a
    deliberate entry in a file rather than the result of somebody not reading far
    enough (open point **H6**).
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = DEFAULT_API_PORT
    token: str = ""
    """Empty means "take it from the `api-token` file, and if it is not there, draw one
    and save it".
    It does not mean "without authentication" — there is no such mode."""


@dataclass(frozen=True, slots=True)
class Config:
    buses: tuple[BusConfig, ...] = ()
    nodes: tuple[NodeConfig, ...] = ()
    bindings: tuple[BindingConfig, ...] = ()
    groups: dict[int, GroupConfig] = field(default_factory=dict)
    accept_new: bool = False
    api: ApiConfig = field(default_factory=ApiConfig)

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(bus.channel for bus in self.buses)

    def node(self, uid: Uid) -> NodeConfig | None:
        for entry in self.nodes:
            if entry.uid == uid:
                return entry
        return None
