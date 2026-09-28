"""A node under an address the hub does not know — 06-cykl-zycia-urzadzenia.md §2.3.

The typical case is not exotic: the hub runs on a Raspberry Pi and comes up minutes after
the nodes, so a node missing from its database never gets the chance to say `Booted` to
it. Everything here is therefore triggered by **ordinary traffic**, not by `Booted`.
"""

from __future__ import annotations

import queue
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

import pytest

from lagunhub.api import ApiError, HubApi, Problem
from lagunhub.core.hub import CoreThread
from lagunhub.core.model import Installation, JoinRecord, NodeModel, NodeState, Uid
from lagunhub.core.rx import ColdItem, Received
from lagunhub.protocol.bus import VirtualBus, VirtualNetwork
from lagunhub.protocol.endpoints import device as dev
from lagunhub.protocol.frame import Frame, encode_id
from lagunhub.protocol.registry import MessageKind, NodeClass
from tests.conftest import Clock
from tests.node.devices import relay1
from tests.node.endpoints.device import short_id
from tests.node.node import NodeCore

UID = bytes(range(8, 16))
ADDRESS = 0x040


@dataclass
class Rig:
    clock: Clock
    node: NodeCore
    hub_port: VirtualBus
    cold: queue.Queue[ColdItem]
    core: CoreThread

    @property
    def installation(self) -> Installation:
        return self.core.installation

    def run(self, seconds: float, step: float = 0.005) -> None:
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.advance(step)
            self.node.poll()
            self.node.tick(self.clock.now)
            while (frame := self.hub_port.recv()) is not None:
                self.cold.put(Received(0, frame))
            self.core.pump(self.clock.now)
            self.node.poll()

    def report(self) -> None:
        """Any frame from the node will do — the mandatory `Diagnostics` report is what
        arrives in real life."""
        self.node.send_report(1, 0x00)
        self.run(0.5)

    def record(self) -> JoinRecord:
        return self.installation.record(short_id(UID))


def build(installation: Installation | None = None, address: int = ADDRESS) -> Rig:
    clock = Clock()
    net = VirtualNetwork()
    node = relay1(uid=UID, address=address, clock=clock, name="stranger")
    node.start(net.attach("node"))
    hub_port = net.attach("hub")
    while hub_port.recv() is not None:
        pass  # the Booted went out before the hub was listening — the usual order
    cold: queue.Queue[ColdItem] = queue.Queue()
    core = CoreThread(
        installation or Installation(), cold, lambda s: None, buses=[hub_port], clock=clock
    )
    return Rig(clock, node, hub_port, cold, core)


class TestRecognition:
    def test_a_report_from_an_unknown_address_makes_it_pending(self) -> None:
        rig = build()
        rig.report()

        record = rig.record()
        assert record.state == NodeState.PENDING
        assert record.address == ADDRESS
        assert record.uid == Uid(UID)

    def test_the_short_id_is_read_not_computed(self) -> None:
        """The hub takes the node's word for it — computing assumes the node computes the
        same way, and ours once did not."""
        rig = build()
        rig.node.device.values[dev.SHORT_ID] = 0x1234_5678
        rig.report()

        assert rig.installation.records[0x1234_5678].address == ADDRESS

    def test_a_pending_node_is_not_let_in_even_with_the_mode_open(self) -> None:
        """The mode is about nodes that announce, i.e. nodes without an address."""
        rig = build()
        rig.core.commissioning.accept_new()
        rig.report()
        rig.run(2.0)

        assert rig.record().state == NodeState.PENDING
        assert Uid(UID) not in rig.installation.nodes

    def test_one_identification_per_address_not_per_frame(self) -> None:
        rig = build()
        for _ in range(5):
            rig.node.send_report(1, 0x00)
        rig.run(0.5)
        rig.report()

        assert rig.core.strangers.probed == 1

    def test_a_silent_one_is_retried_only_after_a_while(self) -> None:
        rig = build()
        rig.node.faults.drop_responses = 100
        rig.report()
        rig.run(1.0)
        assert rig.core.strangers.failed == 1

        rig.node.faults.drop_responses = 0
        rig.report()
        assert rig.core.strangers.probed == 1  # still inside the pause

        rig.run(60.0, step=0.5)
        rig.report()
        assert rig.record().state == NodeState.PENDING

    def test_a_known_address_is_not_probed(self) -> None:
        installation = Installation()
        installation.add(NodeModel(uid=Uid(UID), address=ADDRESS, bus=0, state=NodeState.READY))
        rig = build(installation)
        rig.report()

        assert rig.core.strangers.probed == 0

    def test_a_known_uid_at_another_address_is_an_issue_not_a_pending_node(self) -> None:
        """A backup older than a readdressing: the database and the bus disagree, and the
        hub does not guess which is current."""
        installation = Installation()
        installation.add(NodeModel(uid=Uid(UID), address=0x0AA, bus=0, state=NodeState.READY))
        rig = build(installation)
        rig.report()
        rig.report()

        assert len(rig.installation.issues) == 1
        assert "0x040" in rig.installation.issues[0] and "0x0AA" in rig.installation.issues[0]
        assert rig.core.strangers.probed == 1


class TestHeardAddresses:
    def test_a_heard_address_is_not_handed_out(self) -> None:
        """The database says the address is free, the bus says otherwise — the bus wins
        (03-adresacja.md §6)."""
        rig = build(address=0x001)
        rig.node.faults.drop_responses = 100  # identification fails; hearing is enough
        rig.report()

        assert rig.installation.addresses.allocate(NodeClass.CONTROL) == 0x002

    def test_the_hub_and_broadcast_are_not_heard_as_nodes(self) -> None:
        rig = build()
        rig.cold.put(Received(0, Frame(encode_id(MessageKind.REPORT, 0x7FE, 1, 0), b"\x00")))
        rig.core.pump(rig.clock.now)

        assert rig.core.strangers.probed == 0


class TestOperator:
    def api(self, rig: Rig) -> HubApi:
        def wait(future: Future[Any], timeout: float) -> Any:
            end = rig.clock.now + timeout
            while not future.done() and rig.clock.now < end:
                rig.run(0.02)
            return future.result(timeout=0)

        return HubApi(rig.core, timeout=3.0, wait=wait)

    def test_admitted_it_is_enumerated_under_its_address(self) -> None:
        rig = build()
        rig.report()

        self.api(rig).admit(short_id(UID))
        rig.run(1.0)

        node = rig.installation.nodes[Uid(UID)]
        assert node.state == NodeState.READY
        assert node.address == ADDRESS
        assert rig.record().state == NodeState.READY

    def test_reset_sends_it_back_to_joining(self) -> None:
        rig = build()
        rig.report()

        outcome = self.api(rig).factory_reset(short_id(UID))

        assert outcome.result == "OK"
        assert rig.node.address == 0
        assert rig.record().address == 0
        assert not rig.installation.addresses.taken(ADDRESS)

    def test_only_a_pending_node_with_an_address(self) -> None:
        rig = build()
        rig.installation.record(0xDEAD_BEEF).state = NodeState.PENDING  # waiting to join
        api = self.api(rig)

        with pytest.raises(ApiError) as missing:
            api.admit(0x0BAD_F00D)
        with pytest.raises(ApiError) as unaddressed:
            api.admit(0xDEAD_BEEF)

        assert missing.value.problem == Problem.NOT_FOUND
        assert unaddressed.value.problem == Problem.INVALID
