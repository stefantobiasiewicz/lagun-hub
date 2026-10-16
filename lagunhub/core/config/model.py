"""Hub configuration — what cannot be recovered from the bus.

The installation model is read from the nodes and rebuilds itself within one
`ReportInterval`. **Human decisions never rebuild**: names, bindings, the meaning of
groups, the sources of inputs ([`KONFIGURACJA.md`](../../KONFIGURACJA.md) §1).

Nothing here is **keyed** by address, and nothing will be. An address is a route, not
an identity — a configuration written "to address `0x2A`" falls apart at the first
renumbering. The address a node holds is still **written down**, as a field of its
entry (`JoinedConfig`): the hub assigned it, and a hub that forgets it after a restart
can neither reach a silent node nor keep its address from somebody else
(zadania/zrobione/BUG-05.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from lagunhub.core.model import Uid, Wanted
from lagunhub.protocol.registry import EndpointType, MessageKind

DEFAULT_BITRATE: Final = 500_000
"""W2 settled in favour of the faster one. Throughput is not a factor
(05-mapowanie-can.md §7) — cable length decides, so the value is to be easy to
change."""


MAX_BITRATE: Final = 1_000_000
"""Classic CAN tops out at 1 Mbit/s; LagunBus is CAN 2.0B, so nothing above that."""


class BusKind(Enum):
    """What the hub talks to on the way to a bus — named after the **device** (its
    contract), not after the medium (zadania/FEAT-01.md, R4, R6).

    `socketcan` is any CAN interface the kernel has — an MCP2518FD on SPI, a USB stick,
    a `vcan` for trials — all spoken to with `ip link` (`lagunhub.drivers.socketcan`).
    The USR-CAN315 will be `usr-can315`, not `ethernet`: another box on Ethernet would
    come with another contract (other commands, another frame format), and a kind named
    after the cable would have to hold both. It becomes known together with its
    transport; until then it is an unknown kind rather than an entry that loads and
    carries nothing. The kind is also the path in the API: `/buses/socketcan/{name}`.
    """

    SOCKETCAN = "socketcan"


@dataclass(frozen=True, slots=True)
class BusConfig:
    """One entry in `buses:` — `kind` plus the fields of that kind.

    One flat record and not a class per kind while there is one kind: `channel` is what
    a SocketCAN bus needs. `usr-can315` will add its own fields (address, port) next
    to it, with the loader checking which belong to which.
    """

    name: str
    channel: str
    bitrate: int = DEFAULT_BITRATE
    kind: BusKind = BusKind.SOCKETCAN


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
    attributes: dict[int, Wanted] = field(default_factory=dict)
    """A `float` is in the quantity's units (`Wanted`, zadania/zrobione/DEVICE-01.md N5)."""
    groups: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class JoinedConfig:
    """Where a joined node sits — **written by the hub**, not by a person.

    The only thing in the files that is not a human decision. It is here rather than in
    a database because it is the only part of the installation model that cannot be
    read back from the bus at will: the hub assigned the address, a silent node will not
    say it, and an address forgotten while its node is switched off would be handed to
    another one (lagunbus-spec/07-hub.md §5; zadania/zrobione/BUG-05.md, D1). Everything else the
    model holds — the descriptor, the firmware — enumeration reads again, once, at the
    node's first frame.

    One field holding all three, so that it is plain which part of an entry belongs to
    the hub: a person edits the rest.
    """

    address: int
    bus: str
    """The **name** of the bus in `hub.yaml` — `ground-floor`, not an index that changes
    when a bus is added, nor a channel that changes when the hardware does."""

    short_id: int
    """So that a node back from a factory reset — which announces nothing but its
    `ShortId` — gets its address back without the accepting mode
    (06-cykl-zycia-urzadzenia.md §7)."""


@dataclass(frozen=True, slots=True)
class NodeConfig:
    alias: str
    uid: Uid
    name: str = ""
    room: str = ""
    endpoints: dict[int, EndpointConfig] = field(default_factory=dict)
    blocked: bool = False
    """The hub knows this node and does not serve it (06-cykl-zycia-urzadzenia.md §8).

    A decision about the installation, so it lives here, next to the name — keyed by
    `Uid`, which never changes, unlike the address."""

    joined: JoinedConfig | None = None
    """Set by the hub once the node is enumerated. `None` — a node planned, not joined
    yet, or one the hub has not written down."""


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
    splices: tuple[tuple[int, int, int], ...] = ()
    """Arguments taken from the source event (07-hub.md §3.2): ``(from, to, size)`` —
    `size` bytes at `from` in the event's payload go to `to` in the command's payload,
    both counted from byte 0, the sequence number and the header. Empty: every argument
    is the file's constant. Worked out at load, so the fast path only copies bytes
    (zadania/zrobione/DEVICE-02.md N1)."""


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
