"""A hub with nothing behind it — for testing a client of the hub without a process,
a port or a terminal.

`Hub` is a protocol ([`adapters/client/client.py`](../lagunhub/adapters/client/client.py)), so
anything with its methods will do. The data below is shaped like a small installation:
a relay with an `Output` endpoint and a switch with a `KeyInput`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from lagunhub.api import (
    ActionView,
    ApiError,
    ArgView,
    AttributeView,
    BindingView,
    BusSetupView,
    ChangeView,
    CommandView,
    CommissioningView,
    ConfigView,
    DiffRow,
    EndpointView,
    EnumerateView,
    EventView,
    HealthView,
    HubInfo,
    NodeSummary,
    NodeView,
    Problem,
    ReadoutView,
    ReconcileView,
    RemoveView,
    TrafficView,
)

RELAY = "08090a0b0c0d0e0f"

EP4 = EndpointView(
    number=4,
    type_id=0x0020,
    type_name="Output",
    channel=1,
    name="The lamp over the table",
    attributes=(
        AttributeView(id=0x00, name="OnOff", type="bool", access="RO", value=False, reported=True),
        AttributeView(
            id=0x17,
            name="Purpose",
            type="enum8",
            access="RW",
            value=1,
            label="LIGHT",
            wanted=1,
            wanted_label="LIGHT",
            matches=True,
            choices=("GENERIC", "LIGHT", "VALVE", "INDICATOR"),
            configurable=True,
        ),
        AttributeView(id=0x16, name="AutoOffTime", type="u16", access="RW", configurable=True),
        AttributeView(id=0x01, name="Level", type="u8", access="RW"),
    ),
    commands=(
        CommandView(name="On"),
        CommandView(name="Off"),
        CommandView(name="Toggle"),
        CommandView(
            name="SetLevel",
            args=(
                ArgView(name="level", type="u16", lo=0, hi=65535),
                ArgView(name="transition", type="u16", lo=0, hi=65535),
            ),
            available=False,
        ),
    ),
)


TRAFFIC: tuple[TrafficView, ...] = (
    TrafficView(
        at=61.205,
        bus="hub",
        kind="COMMAND",
        outgoing=True,
        address=0x001,
        endpoint=4,
        element=2,
        name="Toggle",
        tid=1,
    ),
    TrafficView(
        at=61.208,
        bus="hub",
        kind="RESPONSE",
        outgoing=False,
        address=0x001,
        endpoint=4,
        name="Toggle",
        tid=1,
        result="OK",
        elapsed=0.0031,
    ),
    TrafficView(
        at=61.240,
        bus="hub",
        kind="RESPONSE",
        outgoing=False,
        address=0x001,
        endpoint=4,
        name="Purpose",
        tid=2,
        result="BUSY",
        elapsed=0.0028,
    ),
)


SWITCH = "00" * 8

EP0 = EndpointView(
    number=0,
    type_id=0x0001,
    type_name="Device",
    attributes=(AttributeView(id=0x02, name="DeviceType", type="u16", access="RO", value=0x02F0),),
    commands=(
        CommandView(name="Identify", args=(ArgView(name="duration", type="u16", lo=0, hi=65535),)),
        CommandView(name="Reboot", args=(ArgView(name="delay", type="u16", lo=0, hi=65535),)),
    ),
    events=("Booted",),
)
"""A system endpoint: **every one of its commands takes arguments** — and that is exactly
why the chain of two windows, which once broke off, goes through them."""

EP4_KEY = EndpointView(
    number=4,
    type_id=0x0010,
    type_name="KeyInput",
    channel=1,
    name="Left key",
    attributes=(
        AttributeView(id=0x15, name="HoldThreshold", type="u16", access="RW", configurable=True),
        AttributeView(id=0x14, name="HoldEnabled", type="bool", access="RW", configurable=True),
    ),
    events=("Down", "Up", "Click", "HoldStart"),
)


class FakeHub:
    """As much hub as a client sees. Nothing here runs and nothing listens on a port."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.removed: set[str] = set()
        """Nodes `remove` made the hub forget — gone from the list, `NOT_FOUND` by name."""
        self.etag = "sha256:aaa"
        self.binding_rows: tuple[BindingView, ...] = ()
        self.drift: tuple[DiffRow, ...] = (
            DiffRow(
                uid=RELAY,
                alias="kitchen-relay",
                endpoint=4,
                attribute="Purpose",
                state="mismatch",
                wanted=1,
                wanted_label="LIGHT",
                actual=2,
                actual_label="VALVE",
            ),
        )

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def info(self) -> HubInfo:
        return HubInfo(name="lagun-hub", version="0.1.0", nodes=1, ready=1, buses=("ground-floor",))

    def nodes(self) -> tuple[NodeSummary, ...]:
        return tuple(n for n in self._nodes() if n.alias not in self.removed)

    def _nodes(self) -> tuple[NodeSummary, ...]:
        return (
            NodeSummary(
                uid=RELAY,
                alias="kitchen-relay",
                name="Lamp",
                address=0x040,
                bus="ground-floor",
                state="READY",
                endpoints=1,
                configured=True,
            ),
            NodeSummary(uid=SWITCH, alias="hall-switch", configured=True),
        )

    def node(self, ref: str) -> NodeView:
        self._record("node", ref)
        if ref in self.removed:
            raise ApiError(Problem.NOT_FOUND, f"there is no node {ref!r}")
        if ref in ("hall-switch", SWITCH):
            return NodeView(
                uid=SWITCH,
                alias="hall-switch",
                address=0x002,
                state="READY",
                bus="ground-floor",
                detail=(EP0, EP4_KEY),
            )
        if ref not in ("kitchen-relay", RELAY, "Lamp"):
            raise ApiError(Problem.NOT_FOUND, f"there is no node {ref!r}")
        return NodeView(
            uid=RELAY,
            alias="kitchen-relay",
            address=0x040,
            state="READY",
            bus="ground-floor",
            detail=(EP0, EP4),
        )

    def endpoint(self, ref: str, number: int) -> EndpointView:
        self._record("endpoint", ref, number)
        return EP4

    def bindings(self) -> tuple[BindingView, ...]:
        return self.binding_rows

    def issues(self) -> tuple[str, ...]:
        return ()

    def health(self) -> HealthView:
        return HealthView(nodes=1, ready=1, configured=1, issues=0)

    def commissioning(self) -> CommissioningView:
        return CommissioningView(accepting=False)

    def buses(self) -> tuple[BusSetupView, ...]:
        self._record("buses")
        return (self.bus("socketcan", "ground-floor"),)

    def bus(self, kind: str, name: str) -> BusSetupView:
        if (kind, name) != ("socketcan", "ground-floor"):
            raise ApiError(Problem.NOT_FOUND, f"there is no {kind} bus {name!r} in hub.yaml")
        applied = ("bus_apply", (kind, name)) in self.calls
        return BusSetupView(
            name=name,
            kind="socketcan",
            channel="can0",
            bitrate=500_000,
            open=True,
            nodes=1,
            hardware="mcp251xfd",
            state="ok" if applied else "drift",
            changes=()
            if applied
            else (
                ChangeView(
                    "bitrate", "unset", "500 kbit/s", "ip link set can0 type can bitrate 500000"
                ),
            ),
        )

    def bus_apply(self, kind: str, name: str) -> BusSetupView:
        self.bus(kind, name)
        self._record("bus_apply", kind, name)
        return self.bus(kind, name)

    def diff(self) -> tuple[DiffRow, ...]:
        return self.drift

    def command(self, ref: str, endpoint: int, name: str, args: Any = None) -> ActionView:
        self._record("command", ref, endpoint, name, dict(args or {}))
        return ActionView(ok=True, result="OK", attempts=1, elapsed=0.003)

    def write(self, ref: str, endpoint: int, attribute: str, value: Any) -> ActionView:
        self._record("write", ref, endpoint, attribute, value)
        return ActionView(ok=True, result="OK", attempts=1, elapsed=0.003)

    def read(self, ref: str, endpoint: int, attribute: str) -> ActionView:
        self._record("read", ref, endpoint, attribute)
        return ActionView(ok=True, result="OK", attempts=1, elapsed=0.003, data="01")

    def identify(self, ref: str, seconds: int = 5) -> ActionView:
        self._record("identify", ref, seconds)
        return ActionView(ok=True, result="OK", attempts=1, elapsed=0.003)

    def remove(self, ref: str, *, force: bool = False) -> RemoveView:
        self._record("remove", ref, force)
        self.removed.add(ref)
        return RemoveView(uid=RELAY, acknowledged=None if force else True, address=0x040)

    def enumerate(self, ref: str) -> EnumerateView:
        self._record("enumerate", ref)
        return EnumerateView(started=True, running=1)

    def read_all(self, ref: str, endpoint: int | None = None) -> ReadoutView:
        self._record("read_all", ref, endpoint)
        return ReadoutView(started=True, pending=81, running=1)

    def block(self, ref: str) -> ConfigView:
        self._record("block", ref)
        return ConfigView(etag="sha256:bbb")

    def unblock(self, ref: str) -> ConfigView:
        self._record("unblock", ref)
        return ConfigView(etag="sha256:bbb")

    def accept_new(self, seconds: float = 900.0) -> CommissioningView:
        self._record("accept_new", seconds)
        return CommissioningView(accepting=True)

    def admit(self, short_id: int) -> CommissioningView:
        self._record("admit", short_id)
        return CommissioningView(accepting=False)

    def factory_reset(self, short_id: int) -> ActionView:
        self._record("factory_reset", short_id)
        return ActionView(ok=True, result="OK", attempts=1, elapsed=0.003)

    def config(self) -> ConfigView:
        return ConfigView(etag=self.etag, files=("hub.yaml",), document={})

    def config_set_attribute(
        self, ref: str, endpoint: int, attribute: str, value: Any, *, etag: str
    ) -> ConfigView:
        self._record("config_set_attribute", ref, endpoint, attribute, value, etag)
        return ConfigView(etag="sha256:bbb")

    def config_set(self, path: str, value: Any, *, etag: str) -> ConfigView:
        self._record("config_set", path, value, etag)
        return ConfigView(etag="sha256:bbb")

    def config_delete(self, path: str, *, etag: str) -> ConfigView:
        self._record("config_delete", path, etag)
        return ConfigView(etag="sha256:bbb")

    @contextmanager
    def stream(self) -> Iterator[Iterable[EventView]]:
        yield (EventView(kind="state", at=0.0, uid=RELAY, endpoint=4, name="OnOff", value=True),)

    @contextmanager
    def frames(self) -> Iterator[Iterable[TrafficView]]:
        yield TRAFFIC

    def reconcile(self, ref: str) -> ReconcileView:
        self._record("reconcile", ref)
        return ReconcileView(started=True, running=1)

    def config_adopt(self, ref: str, endpoint: int, attribute: str, *, etag: str) -> ConfigView:
        self._record("config_adopt", ref, endpoint, attribute, etag)
        return ConfigView(etag="sha256:ccc")
