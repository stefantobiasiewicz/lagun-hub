"""Removing a node, blocking it, and enumerating it again — 06-cykl-zycia-urzadzenia.md §6,
§8 and §3.3 (zadania/zrobione/FEAT-04.md).

Removing **forgets**: the model, the join record, the entry in the file and every
binding the node takes part in. Blocking **remembers on purpose**: the hub knows the
node and does not serve it. The two used to be one state (`WYCOFANY`), which kept a note
nobody acted on and left no way to keep a device in the installation unserved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lagunhub.api import ApiError, Problem
from lagunhub.core.config import load
from lagunhub.core.model import NodeState, Uid
from lagunhub.protocol.endpoints import device as dev
from lagunhub.protocol.endpoints import key_input as ki
from tests.conftest import Clock
from tests.node.devices import key1, relay1
from tests.node.endpoints.device import short_id
from tests.node.node import NodeCore
from tests.test_api import RELAY_UID, SWITCH_UID, Hub

RELAY = Uid(RELAY_UID)
SWITCH = Uid(SWITCH_UID)


@pytest.fixture
def hub(clock: Clock, tmp_path: Path) -> Hub:
    return Hub(clock, tmp_path)


@pytest.fixture
def relay(hub: Hub, clock: Clock) -> NodeCore:
    node = hub.join(relay1(uid=RELAY_UID, address=0, clock=clock, name="relay"))
    hub.inst.core.commissioning._close()  # the mode is off, as it is most of the time
    return node


@pytest.fixture
def switch(hub: Hub, clock: Clock) -> NodeCore:
    node = hub.join(key1(uid=SWITCH_UID, address=0, clock=clock, name="switch"))
    hub.inst.core.commissioning._close()
    return node


def _source(hub: Hub) -> tuple[Uid, int, int]:
    return (SWITCH, 4, ki.CLICK)


class TestRemove:
    def test_a_confirmed_reset_forgets_everything(self, hub: Hub, relay: NodeCore) -> None:
        address = relay.address
        view = hub.api.remove("kitchen-relay")

        assert view.acknowledged is True
        assert view.address == address
        assert view.unconfigured is True
        assert view.bindings == 1
        inst = hub.inst.installation
        assert RELAY not in inst.nodes
        assert address not in inst.by_address
        assert short_id(RELAY_UID) not in inst.records or (
            inst.records[short_id(RELAY_UID)].uid is None
        ), "the record of the removed node is gone — what is left is a new announcement"
        assert not inst.addresses.taken(address), "acknowledged, so not even heard any more"

    def test_the_file_loses_the_node_and_its_bindings(self, hub: Hub, relay: NodeCore) -> None:
        hub.api.remove("kitchen-relay")

        config = load(hub.path)
        assert config.node(RELAY) is None
        assert config.bindings == (), "a binding to a forgotten alias would break the file"
        assert config.node(SWITCH) is not None

    def test_it_comes_back_only_through_the_mode(self, hub: Hub, relay: NodeCore) -> None:
        """The whole "unplug and plug in again" — 06-cykl-zycia-urzadzenia.md §6."""
        address = relay.address
        hub.api.remove("kitchen-relay")
        hub.inst.run(3.0, step=0.05)

        record = hub.inst.installation.records[short_id(RELAY_UID)]
        assert record.state == NodeState.PENDING
        assert record.address == 0
        assert relay.address == 0

        hub.api.accept_new(60.0)
        hub.inst.run(5.0, step=0.05)

        node = hub.inst.installation.nodes[RELAY]
        assert node.state == NodeState.READY
        assert node.address == address, "the first free one — the same, since it was freed"

    def test_silence_changes_nothing(self, hub: Hub, relay: NodeCore) -> None:
        relay.faults.drop_responses = 9
        with pytest.raises(ApiError) as error:
            hub.api.remove("kitchen-relay")

        assert error.value.problem is Problem.SILENT
        assert "nothing was removed" in error.value.message
        assert "force" in error.value.message
        assert RELAY in hub.inst.installation.nodes
        assert load(hub.path).node(RELAY) is not None

    def test_forced_it_does_not_wait(self, hub: Hub, relay: NodeCore) -> None:
        relay.faults.silent_until = hub.inst.clock.now + 60.0
        address = relay.address

        view = hub.api.remove("kitchen-relay", force=True)

        assert view.acknowledged is None
        assert RELAY not in hub.inst.installation.nodes
        assert load(hub.path).node(RELAY) is None
        assert hub.inst.installation.addresses.taken(address), (
            "heard and never acknowledged — nobody else gets it (03-adresacja.md §6)"
        )

    def test_forced_a_late_acknowledgement_frees_the_address(
        self, hub: Hub, relay: NodeCore
    ) -> None:
        address = relay.address
        hub.api.remove("kitchen-relay", force=True)
        hub.inst.run(1.0)

        assert not hub.inst.installation.addresses.taken(address)

    def test_forced_a_node_never_seen_leaves_only_the_file(self, hub: Hub) -> None:
        view = hub.api.remove("kitchen-relay", force=True)

        assert view.acknowledged is None
        assert view.address == 0
        assert load(hub.path).node(RELAY) is None

    def test_without_force_a_node_never_seen_is_unreachable(self, hub: Hub) -> None:
        with pytest.raises(ApiError) as error:
            hub.api.remove("kitchen-relay")
        assert error.value.problem is Problem.UNAVAILABLE
        assert "force" in error.value.message

    def test_not_while_it_is_being_enumerated(self, hub: Hub, relay: NodeCore) -> None:
        """Enumeration would finish and put the model back."""
        hub.api.enumerate("kitchen-relay")
        with pytest.raises(ApiError) as error:
            hub.api.remove("kitchen-relay", force=True)
        assert error.value.problem is Problem.CONFLICT


class TestEnumerate:
    def test_it_reads_the_node_again(self, hub: Hub, relay: NodeCore) -> None:
        before = hub.inst.installation.nodes[RELAY]
        relay.device.values[dev.FIRMWARE_VERSION] = 0x0002_0000  # an update

        view = hub.api.enumerate("kitchen-relay")
        hub.inst.run(2.0)

        assert view.started is True
        after = hub.inst.installation.nodes[RELAY]
        assert after is not before
        assert after.state == NodeState.READY
        assert after.firmware == 0x0002_0000

    def test_the_old_model_stays_until_the_new_one_is_read(self, hub: Hub, relay: NodeCore) -> None:
        """A failed read must not switch the bindings off."""
        before = hub.inst.installation.nodes[RELAY]
        relay.faults.silent_until = hub.inst.clock.now + 60.0

        hub.api.enumerate("kitchen-relay")
        hub.inst.run(2.0)

        assert hub.inst.installation.nodes[RELAY] is before
        assert any("enumeration" in issue for issue in hub.api.issues()), (
            "a failure the operator asked for has to be told, not swallowed"
        )

    def test_twice_at_once_is_a_conflict(self, hub: Hub, relay: NodeCore) -> None:
        hub.api.enumerate("kitchen-relay")
        with pytest.raises(ApiError) as error:
            hub.api.enumerate("kitchen-relay")
        assert error.value.problem is Problem.CONFLICT

    def test_a_node_without_an_address_cannot_be_read(self, hub: Hub) -> None:
        with pytest.raises(ApiError) as error:
            hub.api.enumerate("kitchen-relay")
        assert error.value.problem is Problem.UNAVAILABLE


class TestBlock:
    def test_it_goes_to_the_file(self, hub: Hub, relay: NodeCore) -> None:
        hub.api.block("kitchen-relay")

        assert load(hub.path).node(RELAY).blocked is True  # type: ignore[union-attr]
        assert hub.inst.installation.nodes[RELAY].state == NodeState.BLOCKED
        assert next(n for n in hub.api.nodes() if n.alias == "kitchen-relay").state == "BLOCKED"

    def test_it_is_not_controlled(self, hub: Hub, relay: NodeCore) -> None:
        hub.api.block("kitchen-relay")
        with pytest.raises(ApiError) as error:
            hub.api.command("kitchen-relay", 4, "Toggle")
        assert error.value.problem is Problem.UNAVAILABLE
        assert "blocked" in error.value.message

    def test_no_binding_targets_it(self, hub: Hub, relay: NodeCore, switch: NodeCore) -> None:
        assert hub.inst.published[-1].bind.get(_source(hub))
        hub.api.block("kitchen-relay")
        assert not hub.inst.published[-1].bind.get(_source(hub))

    def test_its_events_trigger_nothing(self, hub: Hub, relay: NodeCore, switch: NodeCore) -> None:
        hub.api.block("kitchen-switch")
        assert not hub.inst.published[-1].bind.get(_source(hub))

    def test_it_keeps_its_address(self, hub: Hub, relay: NodeCore) -> None:
        address = relay.address
        hub.api.block("kitchen-relay")
        assert hub.inst.installation.addresses.taken(address)
        assert hub.inst.installation.by_address[address] == RELAY

    def test_it_gets_no_address_even_with_the_mode_open(self, hub: Hub, relay: NodeCore) -> None:
        """06-cykl-zycia-urzadzenia.md §8 — the one exception to "a known one always gets
        its address"."""
        hub.api.block("kitchen-relay")
        relay.request_factory_reset()  # by hand, at the device
        hub.api.accept_new(60.0)
        hub.inst.run(3.0, step=0.05)

        assert relay.address == 0
        assert hub.inst.installation.records[short_id(RELAY_UID)].state == NodeState.BLOCKED

    def test_it_is_not_reconciled(self, hub: Hub, relay: NodeCore) -> None:
        hub.api.block("kitchen-relay")
        assert hub.api.reconcile("kitchen-relay").started is False

    def test_unblocked_it_is_read_and_ready_again(self, hub: Hub, relay: NodeCore) -> None:
        hub.api.block("kitchen-relay")
        hub.api.unblock("kitchen-relay")
        hub.inst.run(2.0)

        assert load(hub.path).node(RELAY).blocked is False  # type: ignore[union-attr]
        assert hub.inst.installation.nodes[RELAY].state == NodeState.READY
        assert hub.inst.published[-1].bind.get(_source(hub)) is not None

    def test_a_block_survives_a_restart(self, clock: Clock, tmp_path: Path) -> None:
        """No database — the file is what remembers, so a hub coming up finds the node
        under an address it does not know and recognises it by its `Uid`."""
        first = Hub(clock, tmp_path / "a")
        node = first.join(relay1(uid=RELAY_UID, address=0, clock=clock, name="relay"))
        first.api.block("kitchen-relay")

        second = Hub(clock, tmp_path / "b", content=first.path.read_text(encoding="utf-8"))
        node.stop()
        second.inst.add(node)
        node.send_report(1, 0x00)
        second.inst.run(1.0)

        record = second.inst.installation.records[short_id(RELAY_UID)]
        assert record.state == NodeState.BLOCKED
        assert record.address == node.address
        assert second.inst.installation.addresses.taken(node.address)
        assert not any(row.state == "PENDING" for row in second.api.commissioning().known)

    def test_a_pending_stranger_can_be_blocked_by_uid(self, clock: Clock, tmp_path: Path) -> None:
        """A node the file does not know gets an entry with nothing but the block."""
        hub = Hub(clock, tmp_path)
        node = relay1(uid=bytes(range(32, 40)), address=0x050, clock=clock, name="stranger")
        hub.inst.add(node)
        node.send_report(1, 0x00)
        hub.inst.run(1.0)

        hub.api.block(bytes(range(32, 40)).hex())

        assert load(hub.path).node(Uid(bytes(range(32, 40)))).blocked is True  # type: ignore[union-attr]
        record = hub.inst.installation.records[short_id(bytes(range(32, 40)))]
        assert record.state == NodeState.BLOCKED
