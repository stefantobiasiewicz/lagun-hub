"""Reading everything a node can tell, on an operator's request (zadania/zrobione/FEAT-06.md)."""

from __future__ import annotations

from lagunhub.core.events import Event, EventKind
from lagunhub.core.model import NodeModel, Uid
from lagunhub.core.readout import LONG_TYPES
from lagunhub.protocol.endpoints import key_input as ki
from lagunhub.protocol.registry import CommonAttribute, MessageKind
from tests.conftest import Clock
from tests.node.devices import key1, relay1
from tests.node.endpoints.output import Output
from tests.node.node import NodeCore
from tests.test_enumeration import Rig, joined

SWITCH = Uid(bytes(range(8)))
RELAY = Uid(bytes(range(8, 16)))


def rig_with(clock: Clock, node: NodeCore) -> tuple[Rig, NodeModel, NodeCore]:
    inst = Rig(clock)
    joined(inst, node)
    model = inst.installation.nodes[Uid(node.uid)]
    return inst, model, node


def relay(clock: Clock) -> tuple[Rig, NodeModel, NodeCore]:
    return rig_with(clock, relay1(uid=bytes(RELAY), address=0, clock=clock))


def switch(clock: Clock) -> tuple[Rig, NodeModel, NodeCore]:
    return rig_with(clock, key1(uid=bytes(SWITCH), address=0, clock=clock))


def readable(model: NodeModel, only: int | None = None) -> set[tuple[int, int]]:
    """What the hub should end up knowing: every readable attribute the endpoint has."""
    out: set[tuple[int, int]] = set()
    for number, endpoint in model.endpoints.items():
        definition = endpoint.definition
        if definition is None or (only is not None and number != only):
            continue
        for element, meta in definition.attributes.items():
            if meta.readable and endpoint.lacks(meta) is None:
                out.add((number, element))
    return out


def known(model: NodeModel) -> set[tuple[int, int]]:
    return {
        (number, element)
        for number, endpoint in model.endpoints.items()
        for element in endpoint.attributes
    }


def finished(inst: EventsRig) -> list[Event]:
    return [e for e in inst.events if e.kind is EventKind.NODE and e.name == "READOUT_DONE"]


class EventsRig(Rig):
    """A rig that keeps the events, so a test can see how the readout ended."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock)
        self._subscription = self.core.events.subscribe(depth=10_000)
        self._seen: list[Event] = []

    @property
    def events(self) -> list[Event]:
        while (event := self._subscription.get(0)) is not None:
            self._seen.append(event)
        return self._seen


def watched(clock: Clock, node: NodeCore) -> tuple[EventsRig, NodeModel, NodeCore]:
    inst = EventsRig(clock)
    joined(inst, node)
    return inst, inst.installation.nodes[Uid(node.uid)], node


class TestEverything:
    def test_every_readable_attribute_ends_up_in_the_model(self, clock: Clock) -> None:
        inst, model, _ = watched(clock, relay1(uid=bytes(RELAY), address=0, clock=clock))
        wanted = readable(model)
        assert not wanted <= known(model), "enumeration alone does not know everything"

        total = inst.core.readout.start(model)
        assert total == len(wanted)
        inst.run(3.0)

        absent = sum(len(endpoint.absent) for endpoint in model.endpoints.values())
        assert absent, "relay1 leaves out some optional attributes"
        assert readable(model) <= known(model), "everything but what turned out absent"
        assert not inst.core.readout.busy(RELAY)
        (done,) = finished(inst)
        assert done.uid == RELAY
        assert done.text == f"{total - absent} read, {absent} absent"

    def test_long_values_come_last(self, clock: Clock) -> None:
        """They travel in segments; the short ones are to show up first (R2)."""
        inst, model, _ = relay(clock)
        types: list[str] = []
        original = inst.core._transact

        def spy(bus: int, address: int, endpoint: int, element: int, *rest: object) -> None:
            definition = model.endpoints[endpoint].definition
            assert definition is not None
            types.append(definition.attributes[element].type)
            original(bus, address, endpoint, element, *rest)  # type: ignore[arg-type]

        inst.core.readout._request = spy
        inst.core.readout.start(model)
        inst.run(3.0)

        long = [t in LONG_TYPES for t in types]
        assert any(long)
        assert long == sorted(long), "no short read after the first long one"

    def test_one_endpoint_only(self, clock: Clock) -> None:
        inst, model, _ = relay(clock)
        total = inst.core.readout.start(model, 4)
        assert total == len(readable(model, 4))
        inst.run(2.0)
        assert readable(model, 4) <= known(model)
        assert not readable(model, 0) <= known(model), "ep0 was not asked"

    def test_what_the_endpoint_lacks_is_not_asked(self, clock: Clock) -> None:
        """A feature the endpoint did not declare, an optional attribute already turned
        down — the hub knows without asking."""
        inst, model, _ = switch(clock)
        key = model.endpoints[4]
        key.absent.add(ki.HOLD_TICK_INTERVAL)
        definition = key.definition
        assert definition is not None
        gated = [m for m in definition.attributes.values() if m.feature and key.lacks(m)]
        assert gated, "key1 lacks some feature of KeyInput"

        asked: list[tuple[int, int]] = []
        original = inst.core._transact

        def spy(bus: int, address: int, endpoint: int, element: int, *rest: object) -> None:
            asked.append((endpoint, element))
            original(bus, address, endpoint, element, *rest)  # type: ignore[arg-type]

        inst.core.readout._request = spy
        inst.core.readout.start(model)
        inst.run(3.0)

        assert (4, ki.HOLD_TICK_INTERVAL) not in asked
        assert not {(4, m.id) for m in gated} & set(asked)

    def test_an_optional_attribute_turned_down_is_learned_quietly(self, clock: Clock) -> None:
        """key1 sets `HoldTickInterval` itself — a normal state, no issue (R9)."""
        inst, model, _ = watched(clock, key1(uid=bytes(SWITCH), address=0, clock=clock))
        issues = len(inst.installation.issues)
        inst.core.readout.start(model)
        inst.run(3.0)

        assert ki.HOLD_TICK_INTERVAL in model.endpoints[4].absent
        assert len(inst.installation.issues) == issues
        (done,) = finished(inst)
        assert "absent" in done.text


class TestTrouble:
    def test_mandatory_refusals_make_one_issue(self, clock: Clock) -> None:
        """The node library lacks some common attributes today (zadania/TASK-01.md) — one
        entry for all of them, not one each (R8)."""
        inst, model, node = watched(clock, relay1(uid=bytes(RELAY), address=0, clock=clock))
        spec = node.endpoint(4, Output).spec
        spec.pop(CommonAttribute.Enabled)
        spec.pop(CommonAttribute.QueryEnabled)
        issues = len(inst.installation.issues)

        inst.core.readout.start(model)
        inst.run(3.0)

        new = inst.installation.issues[issues:]
        assert len(new) == 1
        assert "2 mandatory" in new[0]
        assert "ep4 Enabled" in new[0] and "ep4 QueryEnabled" in new[0]
        assert CommonAttribute.Enabled not in model.endpoints[4].absent
        (done,) = finished(inst)
        assert "2 refused" in done.text

    def test_a_silent_node_stops_it(self, clock: Clock) -> None:
        inst, model, node = watched(clock, relay1(uid=bytes(RELAY), address=0, clock=clock))
        node.faults.silent_until = clock.now + 60
        issues = len(inst.installation.issues)

        total = inst.core.readout.start(model)
        inst.run(3.0)

        assert not inst.core.readout.busy(RELAY)
        assert inst.core.transactions.opened  # sanity: something went out
        new = inst.installation.issues[issues:]
        assert len(new) == 1 and "does not answer" in new[0]
        (done,) = finished(inst)
        assert done.text.startswith("stopped at ep0")
        assert f"0 of {total}" in done.text

    def test_a_second_start_is_refused_while_one_runs(self, clock: Clock) -> None:
        inst, model, _ = relay(clock)
        assert inst.core.readout.start(model) > 0
        remaining = inst.core.readout.remaining(RELAY)
        assert remaining is not None and remaining > 0
        assert inst.core.readout.start(model) == 0
        inst.run(3.0)
        assert inst.core.readout.remaining(RELAY) is None


class TestStopping:
    """Removing, blocking or enumerating the node stops the readout (R5)."""

    def test_enumeration_stops_it(self, clock: Clock) -> None:
        inst, model, _ = relay(clock)
        inst.core.readout.start(model)
        inst.run(0.02, step=0.005)
        assert inst.core.readout.busy(RELAY)

        record = inst.installation.records[model.short_id]
        assert inst.core.enumeration.start(record)
        assert not inst.core.readout.busy(RELAY)

    def test_forgetting_stops_it(self, clock: Clock) -> None:
        inst, model, _ = relay(clock)
        inst.core.readout.start(model)
        inst.core.forget(RELAY, acknowledged=False)
        assert not inst.core.readout.busy(RELAY)

    def test_blocking_stops_it(self, clock: Clock) -> None:
        inst, model, _ = relay(clock)
        inst.core.readout.start(model)
        inst.core._apply_blocks(frozenset(), frozenset({RELAY}))
        assert not inst.core.readout.busy(RELAY)

    def test_a_blocked_node_is_not_read(self, clock: Clock) -> None:
        inst, model, _ = relay(clock)
        inst.installation.blocked = frozenset({RELAY})
        assert inst.core.readout.start(model) == 0


def test_reads_go_out_as_reads(clock: Clock) -> None:
    """Nothing but `READ` — reading everything never writes."""
    inst, model, _ = relay(clock)
    kinds: list[MessageKind] = []
    original = inst.core._transact

    def spy(
        bus: int, address: int, endpoint: int, element: int, kind: MessageKind, *rest: object
    ) -> None:
        kinds.append(kind)
        original(bus, address, endpoint, element, kind, *rest)  # type: ignore[arg-type]

    inst.core.readout._request = spy
    inst.core.readout.start(model)
    inst.run(3.0)
    assert kinds and set(kinds) == {MessageKind.READ}
