"""Identifying a device.

Reading from the node is **decisive and the only source**: the registry of models holds no
endpoint lists and never will.
"""

from __future__ import annotations

import queue
from pathlib import Path

from lagunhub.core.config.store import ConfigStore
from lagunhub.core.enumeration import scenes_present
from lagunhub.core.hub import CoreThread
from lagunhub.core.model import Installation, NodeState, Snapshot, Uid
from lagunhub.core.rx import ColdItem, Received
from lagunhub.protocol.bus import VirtualNetwork
from lagunhub.protocol.endpoints import device as dev
from lagunhub.protocol.endpoints import key_input as ki
from lagunhub.protocol.registry import (
    FIRST_FUNCTIONAL_ENDPOINT,
    EndpointType,
    NodeClass,
)
from tests.conftest import Clock
from tests.node.devices import key1, panel3, relay1
from tests.node.node import NodeCore

PANEL_UID = bytes(range(16, 24))
_KEY = ki.Feature.MULTICLICK | ki.Feature.HOLD | ki.Feature.RAW


class Rig:
    """The hub and the nodes on one bus, driven by the test's clock."""

    def __init__(self, clock: Clock, config_store: ConfigStore | None = None) -> None:
        self.clock = clock
        self.net = VirtualNetwork()
        self.hub_port = self.net.attach("hub")
        self.installation = Installation()
        self.cold: queue.Queue[ColdItem] = queue.Queue()
        self.published: list[Snapshot] = []
        self.core = CoreThread(
            self.installation,
            self.cold,
            self.published.append,
            buses=[self.hub_port],
            clock=clock,
            config_store=config_store,
        )
        self.nodes: list[NodeCore] = []

    def add(self, node: NodeCore) -> NodeCore:
        node.start(self.net.attach(node.name))
        self.nodes.append(node)
        return node

    def run(self, seconds: float, step: float = 0.01) -> None:
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.advance(step)
            for node in self.nodes:
                node.poll()
                node.tick(self.clock.now)
            while (frame := self.hub_port.recv()) is not None:
                self.cold.put(Received(0, frame))
            self.core.pump(self.clock.now)
            for node in self.nodes:
                node.poll()


def joined(inst: Rig, node: NodeCore) -> NodeCore:
    """Take a node through joining, identification and reconciling."""
    inst.core.commissioning.accept_new()
    inst.add(node)
    inst.run(5.0, step=0.05)
    return node


class TestDescriptor:
    def test_the_panel_from_the_spec(self, clock: Clock) -> None:
        """The completion criterion for enumeration — the descriptor from 01-model-danych.md §7,
        read from the node."""
        inst = Rig(clock)
        joined(inst, panel3(uid=PANEL_UID, address=0, clock=clock))

        node = inst.installation.nodes[Uid(PANEL_UID)]
        assert node.state == NodeState.READY
        assert node.device_type == 0x01F1
        assert node.vendor_id == 0

        description = {
            number: (endpoint.type_id, endpoint.channel, endpoint.features)
            for number, endpoint in sorted(node.endpoints.items())
            if number >= FIRST_FUNCTIONAL_ENDPOINT
        }
        assert description == {
            4: (EndpointType.KeyInput, 1, _KEY),
            5: (EndpointType.KeyInput, 2, _KEY),
            6: (EndpointType.KeyInput, 3, _KEY),
            7: (EndpointType.Output, 0, 0b11),
            8: (EndpointType.Sense, 0, 0),
        }


class TestSystemEndpoints:
    """The specification fixes the numbers and types, so the hub **does not ask** for
    them — it puts them in.

    Before the shell it did not put them in either: it knew `Device`'s contents, because it
    had read them, but had nowhere to put them. That came out only at the question "which
    commands can I send", because `cmd <node> ep0 Reboot` answered "there is no ep0".
    """

    def test_every_node_has_ep0_ep1_ep2(self, clock: Clock) -> None:
        inst = Rig(clock)
        joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock))

        node = inst.installation.nodes[Uid(bytes(range(8)))]
        type_ids = {n: node.endpoints[n].type_id for n in (0, 1, 2)}
        assert type_ids == {
            0: EndpointType.Device,
            1: EndpointType.Diagnostics,
            2: EndpointType.Groups,
        }

    def test_scenes_only_where_there_is_an_output(self, clock: Clock) -> None:
        """The rule can be worked out from the descriptor, so nothing has to be asked
        (01-model-danych.md §1.2)."""
        inst = Rig(clock)
        joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock))
        joined(inst, relay1(uid=bytes(range(8, 16)), address=0, clock=clock))

        key_sim = inst.installation.nodes[Uid(bytes(range(8)))]
        relay = inst.installation.nodes[Uid(bytes(range(8, 16)))]
        assert 3 not in key_sim.endpoints, "a key has nothing to drive"
        assert relay.endpoints[3].type_id == EndpointType.Scenes

    def test_the_device_that_was_read_has_somewhere_to_sit(self, clock: Clock) -> None:
        """Enumeration reads it anyway — without ep0 in the model it was lost beyond the
        header fields."""
        inst = Rig(clock)
        joined(inst, panel3(uid=PANEL_UID, address=0, clock=clock))

        ep0 = inst.installation.nodes[Uid(PANEL_UID)].endpoints[0]
        assert ep0.attributes[dev.DEVICE_TYPE].value == 0x01F1
        assert ep0.attributes[dev.ENDPOINT_COUNT].value == 5

    def test_system_endpoints_cost_not_a_single_transaction(self, clock: Clock) -> None:
        """If the hub asked for them, every node would cost sixteen reads more."""
        inst = Rig(clock)
        joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock))

        node = inst.installation.nodes[Uid(bytes(range(8)))]
        system_eps = [n for n in node.endpoints if n < FIRST_FUNCTIONAL_ENDPOINT]
        assert system_eps == [0, 1, 2]
        for number in system_eps:
            assert node.endpoints[number].revision == 0, "nobody read the revision"

    def test_the_uid_arrived_in_segments(self, clock: Clock) -> None:
        inst = Rig(clock)
        joined(inst, panel3(uid=PANEL_UID, address=0, clock=clock))

        assert bytes(Uid(PANEL_UID)) in {bytes(u) for u in inst.installation.nodes}
        assert inst.core.segments.completed >= 1, "a Uid does not fit in a frame"

    def test_the_record_joins_up_with_the_model(self, clock: Clock) -> None:
        """Only enumeration joins a ShortId to a Uid — two tables become one whole."""
        inst = Rig(clock)
        node = panel3(uid=PANEL_UID, address=0, clock=clock)
        joined(inst, node)

        record = inst.installation.record(node.device.num(dev.SHORT_ID))
        assert record.uid == Uid(PANEL_UID)
        assert record.state == NodeState.READY
        assert inst.installation.by_address[record.address] == Uid(PANEL_UID)

    def test_scenes_is_derived_from_the_descriptor(self, clock: Clock) -> None:
        """01-model-danych.md §1.2 — system endpoints are not asked about."""
        inst = Rig(clock)
        joined(inst, panel3(uid=PANEL_UID, address=0, clock=clock))
        joined(inst, key1(uid=bytes(range(24, 32)), address=0, clock=clock, name="k"))

        panel = inst.installation.nodes[Uid(PANEL_UID)]
        key_sim = inst.installation.nodes[Uid(bytes(range(24, 32)))]
        assert scenes_present(panel), "the panel has backlighting, that is, an Output"
        assert not scenes_present(key_sim), "a bare key has no output at all"

    def test_there_are_as_many_reads_as_the_spec_promises(self, clock: Clock) -> None:
        """Four transactions per endpoint plus a few per node
        (06-cykl-zycia-urzadzenia.md §3.1)."""
        inst = Rig(clock)
        joined(inst, panel3(uid=PANEL_UID, address=0, clock=clock))

        assert inst.core.enumeration.completed == 1
        assert inst.core.enumeration.failed == 0
        assert inst.core.transactions.opened == 7 + 5 * 4
        assert inst.core.transactions.retried == 0


class TestReEnumeration:
    def test_the_same_firmware_version_does_not_trigger_it(self, clock: Clock) -> None:
        inst = Rig(clock)
        node = panel3(uid=PANEL_UID, address=0, clock=clock)
        joined(inst, node)
        assert inst.core.enumeration.completed == 1

        node.request_reboot(0.0)
        inst.run(3.0, step=0.05)
        assert inst.core.enumeration.started == 1, "the same version — there is nothing to read"

    def test_a_different_firmware_version_triggers_it(self, clock: Clock) -> None:
        """Booted carries firmwareVersion, so the hub decides without asking
        (06-cykl-zycia-urzadzenia.md §3.3)."""
        inst = Rig(clock)
        node = panel3(uid=PANEL_UID, address=0, clock=clock)
        joined(inst, node)

        node.device.values[dev.FIRMWARE_VERSION] = 0x0002_0000
        node.request_reboot(0.0)
        inst.run(3.0, step=0.05)

        assert inst.core.enumeration.completed == 2
        assert inst.installation.nodes[Uid(PANEL_UID)].firmware == 0x0002_0000


class TestFailedIdentification:
    def test_a_node_that_does_not_answer(self, clock: Clock) -> None:
        """Three attempts without an answer end the identification — the model stays
        untouched."""
        inst = Rig(clock)
        record = inst.installation.record(0xDEAD_BEEF)
        record.address = 0x055  # there is nobody at this address
        record.state = NodeState.JOINED

        assert inst.core.enumeration.start(record)
        inst.run(1.0, step=0.05)

        assert inst.core.enumeration.failed == 1
        assert inst.installation.nodes == {}
        assert record.state == NodeState.ABSENT
        assert record.uid is None

    def test_falling_silent_midway_aborts_it(self, clock: Clock) -> None:
        inst = Rig(clock)
        node = panel3(uid=PANEL_UID, address=0, clock=clock)
        inst.core.commissioning.accept_new()
        inst.add(node)
        inst.run(1.85, step=0.05)  # announcement and assignment, enumeration in progress
        assert len(inst.core.enumeration) == 1, "the identification is in progress"

        node.faults.silent_until = clock.now + 60.0
        inst.run(1.0, step=0.05)

        assert inst.core.enumeration.failed == 1
        assert Uid(PANEL_UID) not in inst.installation.nodes


def test_a_relay_is_identified_too(clock: Clock) -> None:
    inst = Rig(clock)
    joined(inst, relay1(uid=bytes(range(8, 16)), address=0, clock=clock))

    node = inst.installation.nodes[Uid(bytes(range(8, 16)))]
    assert node.endpoints[4].type_id == EndpointType.Output
    assert node.endpoints[4].channel == 1
    assert scenes_present(node)


def test_the_node_class_does_not_reach_the_model(clock: Clock) -> None:
    """`NodeClass` is not an attribute and cannot be read (05-mapowanie-can.md §4.1).

    It lives only in the announcement frame — what a device is, the descriptor says.
    """
    inst = Rig(clock)
    node = panel3(uid=PANEL_UID, address=0, clock=clock)
    joined(inst, node)

    record = inst.installation.record(node.device.num(dev.SHORT_ID))
    assert record.node_class == NodeClass.CONTROL, "the hint stays in the record"
    assert not hasattr(inst.installation.nodes[Uid(PANEL_UID)], "node_class")


class TestHubRestart:
    """07-hub.md §5.1 — after a restart the hub **listens, it does not poll**."""

    def test_the_model_comes_back_from_the_database_and_nobody_is_polled(
        self, clock: Clock, tmp_path: Path
    ) -> None:
        from lagunhub.core.store import SqliteStore

        inst = Rig(clock)
        node = panel3(uid=PANEL_UID, address=0, clock=clock)
        joined(inst, node)
        assert inst.core.enumeration.completed == 1

        store = SqliteStore(tmp_path / "hub.db")
        store.save(inst.installation)

        # --- the hub comes up afresh, the node still hangs on the bus ---
        after = Rig(clock)
        after.installation = store.load()
        after.core = CoreThread(
            after.installation,
            after.cold,
            after.published.append,
            buses=[after.hub_port],
            clock=clock,
        )
        after.nodes = inst.nodes
        for n in after.nodes:
            n.bus = after.net.attach(f"{n.name}-after")

        after.run(2.0, step=0.05)

        assert after.installation.nodes[Uid(PANEL_UID)].device_type == 0x01F1
        assert after.core.transactions.opened == 0, "after a restart the hub polled nobody"
        assert after.core.enumeration.started == 0

    def test_but_a_node_restart_with_new_firmware_does_poll(
        self, clock: Clock, tmp_path: Path
    ) -> None:
        from lagunhub.core.store import SqliteStore

        inst = Rig(clock)
        node = panel3(uid=PANEL_UID, address=0, clock=clock)
        joined(inst, node)

        store = SqliteStore(tmp_path / "hub.db")
        store.save(inst.installation)

        after = Rig(clock)
        after.installation = store.load()
        after.core = CoreThread(
            after.installation,
            after.cold,
            after.published.append,
            buses=[after.hub_port],
            clock=clock,
        )
        after.nodes = inst.nodes
        for n in after.nodes:
            n.bus = after.net.attach(f"{n.name}-after")

        node.device.values[dev.FIRMWARE_VERSION] = 0x0002_0000
        node.request_reboot(0.0)
        after.run(3.0, step=0.05)

        assert after.core.enumeration.completed == 1
        assert after.installation.nodes[Uid(PANEL_UID)].firmware == 0x0002_0000


class TestIdentityConflict:
    """The same node answering from two addresses.

    The report of this conflict existed from the start of enumeration and all that time **had no
    recipient**: `uid_conflicts` was a list one only ever appended to. Nobody noticed it
    carried useless content — the same `Uid` twice instead of the old address — because
    nobody read it. Hence this test: it watches the road all the way to an issue a person
    will see.
    """

    def test_the_same_uid_at_two_addresses_is_an_issue(self, clock: Clock) -> None:
        inst = Rig(clock)
        node = joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock))
        uid = Uid(bytes(node.uid))
        old = inst.installation.nodes[uid].address
        assert old

        # The same node announces itself again, but now from another address — that is
        # what a readdressing whose confirmation went missing looks like.
        inst.core._on_uid_conflict(old + 1, uid, old)

        assert len(inst.installation.issues) == 1
        issue = inst.installation.issues[0]
        assert uid.hex() in issue
        # The old address has to be in the text — without it there is nothing to
        # diagnose from.
        assert f"0x{old:03X}" in issue
        assert f"0x{old + 1:03X}" in issue

    def test_the_report_does_not_multiply(self, clock: Clock) -> None:
        inst = Rig(clock)
        node = joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock))
        uid = Uid(bytes(node.uid))
        old = inst.installation.nodes[uid].address

        for _ in range(4):
            inst.core._on_uid_conflict(old + 1, uid, old)

        assert len(inst.installation.issues) == 1
        assert len(inst.core.uid_conflicts) == 1
