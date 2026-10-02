"""Persistence of the model.

The database is **a backup and a starting point, never a source of answers** while
running.
"""

from __future__ import annotations

from pathlib import Path

from lagunhub.core.model import (
    AttributeState,
    EndpointModel,
    Installation,
    NodeModel,
    NodeState,
    Uid,
)
from lagunhub.core.store import SqliteStore, StoreThread
from lagunhub.protocol.registry import EndpointType, NodeClass
from tests.conftest import Clock

PANEL = Uid(bytes(range(16, 24)))
RELAY = Uid(bytes(range(8, 16)))


def setup() -> Installation:
    inst = Installation()
    panel = inst.add(
        NodeModel(
            uid=PANEL,
            address=0x012,
            bus=0,
            state=NodeState.READY,
            short_id=0xAABBCCDD,
            vendor_id=0,
            device_type=0x01F1,
            firmware=0x0001_0000,
            name="Living-room panel",
        )
    )
    panel.endpoints[4] = EndpointModel(4, EndpointType.KeyInput, revision=1, channel=1)
    panel.endpoints[7] = EndpointModel(7, EndpointType.Output, revision=1, features=0b11)
    panel.endpoints[9] = EndpointModel(9, None, revision=0)  # a vendor type
    inst.add(NodeModel(uid=RELAY, address=0x020, bus=1, state=NodeState.ABSENT))

    record = inst.record(0xAABBCCDD)
    record.uid = PANEL
    record.address = 0x012
    record.node_class = NodeClass.CONTROL
    record.state = NodeState.READY
    inst.record(0x11112222).state = NodeState.BLOCKED
    return inst


class TestSaveAndLoad:
    def test_the_model_survives_a_restart(self, tmp_path: Path) -> None:
        store = SqliteStore(tmp_path / "hub.db")
        store.save(setup())
        store.close()

        restored = SqliteStore(tmp_path / "hub.db").load()

        panel = restored.nodes[PANEL]
        assert panel.address == 0x012
        assert panel.device_type == 0x01F1
        assert panel.firmware == 0x0001_0000
        assert restored.nodes[RELAY].state == NodeState.ABSENT

    def test_the_name_does_NOT_come_back_from_the_database(self, tmp_path: Path) -> None:
        """A name is a human decision and lives in the configuration (KONFIGURACJA.md §1).

        Written in the database as well it would be a second source of truth — and a hub
        that lost its files would show an installation with names and without bindings,
        that is, **working in appearance only**. The loss is to be visible.
        """
        store = SqliteStore(tmp_path / "hub.db")
        store.save(setup())
        restored = store.load()

        assert restored.nodes[PANEL].name == ""
        assert restored.nodes[PANEL].device_type == 0x01F1, "what was read comes back"

    def test_the_descriptor_survives_a_restart(self, tmp_path: Path) -> None:
        store = SqliteStore(tmp_path / "hub.db")
        store.save(setup())
        restored = store.load()

        endpoints = restored.nodes[PANEL].endpoints
        assert endpoints[4].type_id == EndpointType.KeyInput
        assert endpoints[4].channel == 1
        assert endpoints[7].features == 0b11
        assert endpoints[9].type_id is None, "an unknown type stays unknown"

    def test_join_records_survive(self, tmp_path: Path) -> None:
        """A state other than READY has to survive too — `BLOCKED` is the one that
        matters, since a blocked node keeps its address."""
        store = SqliteStore(tmp_path / "hub.db")
        store.save(setup())
        restored = store.load()

        assert restored.records[0xAABBCCDD].uid == PANEL
        assert restored.records[0xAABBCCDD].node_class == NodeClass.CONTROL
        assert restored.records[0x11112222].state == NodeState.BLOCKED

    def test_addresses_return_to_the_pool_as_taken(self, tmp_path: Path) -> None:
        store = SqliteStore(tmp_path / "hub.db")
        store.save(setup())
        restored = store.load()

        assert restored.addresses.taken(0x012)
        assert restored.addresses.taken(0x020)
        assert restored.addresses.allocate(NodeClass.CONTROL) != 0x012

    def test_attribute_state_does_NOT_come_back(self, tmp_path: Path) -> None:
        """07-hub.md §5.2 — one must not assume outputs are in the state they were before
        the restart.

        The nodes may have been switched by hand, may have restarted, or `AutoOffTime`
        may have fired. The truth is the first report that arrives.
        """
        inst = setup()
        inst.nodes[PANEL].endpoints[7].attributes[0x00] = AttributeState(b"\x01", True, 1.0)

        store = SqliteStore(tmp_path / "hub.db")
        store.save(inst)
        restored = store.load()

        assert restored.nodes[PANEL].endpoints[7].attributes == {}

    def test_an_empty_database_yields_an_empty_model(self, tmp_path: Path) -> None:
        assert SqliteStore(tmp_path / "fresh.db").load().nodes == {}

    def test_a_save_replaces_the_previous_one(self, tmp_path: Path) -> None:
        store = SqliteStore(tmp_path / "hub.db")
        store.save(setup())

        smaller = Installation()
        smaller.add(NodeModel(uid=RELAY, address=0x020, bus=0))
        store.save(smaller)

        assert set(store.load().nodes) == {RELAY}


class TestStoreThread:
    def test_save_requests_coalesce(self, tmp_path: Path) -> None:
        """In a wave of joinings we write once, not forty times."""
        store = SqliteStore(tmp_path / "hub.db")
        thread = StoreThread(store)
        inst = setup()

        for _ in range(20):
            thread.request_save(inst)
        thread.start()
        thread.stop()
        thread.join(timeout=5.0)

        assert thread.saved < 20, "some of the requests ran together into one"
        assert thread.coalesced > 0
        assert set(store.load().nodes) == {PANEL, RELAY}

    def test_core_saves_after_the_model_changes(self, tmp_path: Path, clock: Clock) -> None:
        import queue

        from lagunhub.core.hub import CoreThread
        from lagunhub.core.rx import ColdItem

        store = SqliteStore(tmp_path / "hub.db")
        thread = StoreThread(store)
        thread.start()

        inst = Installation()
        cold: queue.Queue[ColdItem] = queue.Queue()
        core = CoreThread(inst, cold, lambda _s: None, store=thread, clock=clock)

        def add(i: Installation) -> None:
            i.add(NodeModel(uid=RELAY, address=0x020, bus=0))

        core.call(add)
        core.pump()

        thread.stop()
        thread.join(timeout=5.0)
        assert set(store.load().nodes) == {RELAY}
