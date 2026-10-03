"""`HubApi` — the named list of operations every adapter will stand on.

The tests are here to keep **three promises**, because breaking any of them shows up only
in an adapter, and by then it is too late:

1. the operations return data, not strings,
2. the data goes through `json.dumps` without a translator,
3. everything — reading included — goes through the CORE thread.
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from lagunhub.api import ApiError, HubApi, Problem
from lagunhub.core.config import load
from lagunhub.core.config.apply import apply
from lagunhub.core.config.store import ConfigStore
from lagunhub.core.model import Uid
from lagunhub.drivers.base import Rejected, Unreachable
from lagunhub.drivers.socketcan import SocketCanSettings
from lagunhub.protocol.endpoints import key_input as ki
from lagunhub.protocol.endpoints import output as out
from lagunhub.protocol.registry import CommonAttribute
from tests.conftest import Clock
from tests.node.devices import key1, relay1
from tests.node.endpoints.key_input import KeyInput
from tests.node.endpoints.output import Output
from tests.node.node import NodeCore
from tests.test_buses import BITRATE, LINK, MISSING, FakeDriver
from tests.test_enumeration import Rig, joined

SWITCH_UID = bytes(range(8))
RELAY_UID = bytes(range(8, 16))

EMPTY = """
buses: [vcan0]
nodes: {}
bindings: []
"""
"""A configuration without a single node — that is what a fresh installation looks
like."""

CONFIG = """
buses: [vcan0]
groups:
  1: { name: all-lights }
nodes:
  "0001020304050607":
    alias: kitchen-switch
    name: "The kitchen key"
    room: kitchen
    endpoints:
      4: { type: KeyInput, HoldThreshold: 600, ReportInterval: 60 }
  "08090a0b0c0d0e0f":
    alias: kitchen-relay
    name: "The lamp over the table"
    room: kitchen
    endpoints:
      4: { type: Output, Purpose: LIGHT, groups: [all-lights] }
bindings:
  - from: { node: kitchen-switch, endpoint: 4, type: KeyInput, event: Click }
    to:   { node: kitchen-relay,  endpoint: 4, type: Output,   command: Toggle }
"""


class Hub:
    """An installation with the API attached. The clock is driven from the test, so `wait`
    does not block but **winds the world on** until the answer arrives."""

    def __init__(self, clock: Clock, tmp_path: Path, content: str = CONFIG) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        path = tmp_path / "config.yaml"
        path.write_text(content, encoding="utf-8")
        self.path = path
        self.inst = Rig(clock, config_store=ConfigStore(path))
        apply(self.inst.installation, load(path))
        self.api = HubApi(self.inst.core, timeout=2.0, wait=self._wait)

    def _wait(self, future: Future[Any], timeout: float) -> Any:
        end = self.inst.clock.now + timeout
        while not future.done() and self.inst.clock.now < end:
            self.inst.run(0.02, step=0.005)
        if not future.done():
            raise FutureTimeout
        return future.result(timeout=0)

    def join(self, node: NodeCore) -> NodeCore:
        return joined(self.inst, node)


@pytest.fixture
def hub(clock: Clock, tmp_path: Path) -> Hub:
    return Hub(clock, tmp_path)


@pytest.fixture
def relay(hub: Hub, clock: Clock) -> NodeCore:
    return hub.join(relay1(uid=RELAY_UID, address=0, clock=clock, name="relay"))


class TestReading:
    def test_the_list_includes_nodes_not_yet_present(self, hub: Hub) -> None:
        """A configured but unjoined node **has to** be visible — otherwise a typo in a
        `Uid` looks like a broken node."""
        items = hub.api.nodes()
        assert [n.alias for n in items] == ["kitchen-switch", "kitchen-relay"]
        assert all(n.configured for n in items)
        assert all(n.state == "UNKNOWN" and n.address == 0 for n in items)

    def test_a_joined_node_has_an_address_and_a_state(self, hub: Hub, relay: NodeCore) -> None:
        view = next(n for n in hub.api.nodes() if n.alias == "kitchen-relay")
        assert view.state == "READY"
        assert view.address == relay.address
        assert view.bus == "hub"
        assert view.room == "kitchen"
        assert view.endpoints > 0

    def test_the_node_view_shows_configuration_next_to_actual(
        self, hub: Hub, relay: NodeCore
    ) -> None:
        """The row that made writing the API early worth it (ARCHITEKTURA.md §10.6)."""
        view = hub.api.node("kitchen-relay")
        ep4 = next(e for e in view.detail if e.number == 4)
        purpose = next(a for a in ep4.attributes if a.name == "Purpose")

        assert purpose.wanted == out.Purpose.LIGHT
        assert purpose.wanted_label == "LIGHT"
        assert purpose.label == "LIGHT", "the actual value has a name too, not only a number"
        assert purpose.matches is True, "reconciling has already written it over"
        assert purpose.configurable is True

    def test_state_without_configuration_does_not_fake_agreement(
        self, hub: Hub, relay: NodeCore
    ) -> None:
        onoff = self._onoff(hub)
        assert onoff.wanted is None
        assert onoff.matches is None, "no configuration is not agreement"
        assert onoff.configurable is False, "OnOff is state, not configuration"

    def test_an_unknown_value_is_not_a_default_value(self, hub: Hub, relay: NodeCore) -> None:
        """The hub does not poll nodes after they join and there is no reason to change
        that — state is restored from reports (01-model-danych.md §6). Until a report
        arrives the honest answer is "I do not know", not `False`."""
        assert self._onoff(hub).value is None
        assert self._onoff(hub).age is None

        hub.api.command("kitchen-relay", 4, "Toggle")
        hub.inst.run(0.2, step=0.01)

        onoff = self._onoff(hub)
        assert onoff.value is True, "the report of the change arrived and the hub knows now"
        assert onoff.age is not None

    @staticmethod
    def _onoff(hub: Hub) -> Any:
        ep4 = next(e for e in hub.api.node("kitchen-relay").detail if e.number == 4)
        return next(a for a in ep4.attributes if a.name == "OnOff")

    def test_a_common_attribute_is_in_the_view(self, hub: Hub, clock: Clock) -> None:
        """`ReportInterval` belongs to the common range, not to the type — and is to be
        visible the same as any other."""
        hub.join(key1(uid=SWITCH_UID, address=0, clock=clock, name="sw"))
        ep4 = next(e for e in hub.api.node("kitchen-switch").detail if e.number == 4)
        interval = next(a for a in ep4.attributes if a.name == "ReportInterval")
        assert interval.id == CommonAttribute.ReportInterval
        assert interval.wanted == 60
        assert interval.matches is True

    def test_named_values_are_exposed(self, hub: Hub, relay: NodeCore) -> None:
        """A front end is to take its drop-down list from the hub, not from a table
        transcribed at its own end."""
        ep4 = next(e for e in hub.api.node("kitchen-relay").detail if e.number == 4)
        purpose = next(a for a in ep4.attributes if a.name == "Purpose")
        assert "LIGHT" in purpose.choices
        assert "INDICATOR" in purpose.choices

    def test_an_unknown_node_says_so_outright(self, hub: Hub) -> None:
        with pytest.raises(ApiError) as error:
            hub.api.node("cellar")
        assert error.value.problem is Problem.NOT_FOUND

    def test_a_node_may_be_named_three_ways(self, hub: Hub, relay: NodeCore) -> None:
        by_alias = hub.api.node("kitchen-relay")
        assert hub.api.node(RELAY_UID.hex()).uid == by_alias.uid
        assert hub.api.node("The lamp over the table").uid == by_alias.uid


class TestControl:
    def test_a_command_reaches_the_node(self, hub: Hub, relay: NodeCore) -> None:
        assert relay.endpoint(4, Output).values[out.ON_OFF] is False
        result = hub.api.command("kitchen-relay", 4, "Toggle")
        assert result.ok and result.result == "OK"
        assert relay.endpoint(4, Output).values[out.ON_OFF] is True

    def test_a_setting_reaches_the_node(self, hub: Hub, relay: NodeCore) -> None:
        hub.api.write("kitchen-relay", 4, "PowerOnBehaviour", "RESTORE")
        assert relay.endpoint(4, Output).values[out.POWER_ON_BEHAVIOUR] == 2

    def test_a_read_goes_to_the_node(self, hub: Hub, relay: NodeCore) -> None:
        hub.api.command("kitchen-relay", 4, "Toggle")
        result = hub.api.read("kitchen-relay", 4, "OnOff")
        assert result.ok and result.data == "01"

    def test_a_read_learns_an_optional_attribute_the_node_lacks(self, hub: Hub) -> None:
        """The refusal lands in the model the way every reply does, so a read teaches the
        hub as much as reconciling does (zadania/zrobione/FEAT-06.md, R9)."""
        hub.join(key1(uid=SWITCH_UID, address=0, clock=hub.inst.clock, name="sw"))
        with pytest.raises(ApiError) as first:
            hub.api.read("kitchen-switch", 4, "HoldTickInterval")
        assert first.value.problem is Problem.REFUSED
        switch = hub.inst.installation.nodes[Uid(SWITCH_UID)]
        assert ki.HOLD_TICK_INTERVAL in switch.endpoints[4].absent

        before = hub.api.health().transactions["opened"]
        with pytest.raises(ApiError) as second:
            hub.api.read("kitchen-switch", 4, "HoldTickInterval")
        assert second.value.problem is Problem.INVALID
        assert "does not expose" in second.value.message
        assert hub.api.health().transactions["opened"] == before, "the node is not asked again"

    def test_a_refused_mandatory_attribute_is_not_taken_for_absent(
        self, hub: Hub, relay: NodeCore
    ) -> None:
        """A device may leave out only what the type calls optional. Refusing a mandatory
        one is the node's fault, and calling it "set by the device itself" would lie
        (zadania/zrobione/FEAT-06.md, R8)."""
        # The node library's functional endpoints lack `Enabled` today (zadania/TASK-01.md).
        relay.endpoint(4, Output).spec.pop(CommonAttribute.Enabled)
        with pytest.raises(ApiError) as error:
            hub.api.read("kitchen-relay", 4, "Enabled")
        assert error.value.result == "UNSUPPORTED_ELEMENT"
        node = hub.inst.installation.nodes[Uid(RELAY_UID)]
        assert CommonAttribute.Enabled not in node.endpoints[4].absent

    def test_identify_needs_no_knowledge_of_endpoints(self, hub: Hub, relay: NodeCore) -> None:
        assert hub.api.identify("kitchen-relay", seconds=3).ok

    def test_a_node_without_an_address_does_not_pretend_to_work(self, hub: Hub) -> None:
        with pytest.raises(ApiError) as error:
            hub.api.command("kitchen-relay", 4, "Toggle")
        assert error.value.problem is Problem.UNAVAILABLE

    def test_a_typo_in_a_command_says_what_is_known(self, hub: Hub, relay: NodeCore) -> None:
        with pytest.raises(ApiError) as error:
            hub.api.command("kitchen-relay", 4, "Toogle")
        assert error.value.problem is Problem.NOT_FOUND
        assert "Toggle" in error.value.message

    def test_a_value_out_of_range_never_reaches_the_bus(self, hub: Hub, relay: NodeCore) -> None:
        before = hub.api.health().transactions["opened"]
        with pytest.raises(ApiError) as error:
            hub.api.write("kitchen-relay", 4, "Level", 300)
        assert error.value.problem is Problem.INVALID
        assert hub.api.health().transactions["opened"] == before

    def test_a_refusal_from_a_node_differs_from_silence(self, hub: Hub, relay: NodeCore) -> None:
        relay.faults.busy_responses = 9
        with pytest.raises(ApiError) as error:
            hub.api.command("kitchen-relay", 4, "Toggle")
        assert error.value.problem is Problem.REFUSED
        assert error.value.result == "BUSY"

    def test_silence_from_a_node_differs_from_a_refusal(self, hub: Hub, relay: NodeCore) -> None:
        relay.faults.drop_responses = 9
        with pytest.raises(ApiError) as error:
            hub.api.command("kitchen-relay", 4, "Toggle")
        assert error.value.problem is Problem.SILENT
        assert "3 attempts" in error.value.message

    def test_joining_mode_expires_on_its_own(self, hub: Hub) -> None:
        assert hub.api.commissioning().accepting is False
        assert hub.api.accept_new(60.0).accepting is True
        hub.inst.run(90.0, step=1.0)
        assert hub.api.commissioning().accepting is False


class TestReadingEverything:
    """`read_all` — every readable attribute, in the background (zadania/zrobione/FEAT-06.md)."""

    def test_it_returns_at_once_and_fills_the_node_view(self, hub: Hub, relay: NodeCore) -> None:
        before = hub.api.node("kitchen-relay")
        view = hub.api.read_all("kitchen-relay")
        assert view.started and view.pending > 0

        during = hub.api.node("kitchen-relay")
        assert during.reading is not None and 0 < during.reading <= view.pending, "not waited"

        hub.inst.run(3.0)
        after = hub.api.node("kitchen-relay")
        assert after.reading is None
        empty = sum(1 for e in before.detail for a in e.attributes if a.value is None)
        still = sum(1 for e in after.detail for a in e.attributes if a.value is None)
        assert still < empty

    def test_one_endpoint(self, hub: Hub, relay: NodeCore) -> None:
        whole = hub.api.read_all("kitchen-relay")
        hub.inst.run(3.0)
        one = hub.api.read_all("kitchen-relay", endpoint=4)
        assert 0 < one.pending < whole.pending

    def test_a_second_one_is_a_conflict(self, hub: Hub, relay: NodeCore) -> None:
        hub.api.read_all("kitchen-relay")
        with pytest.raises(ApiError) as error:
            hub.api.read_all("kitchen-relay")
        assert error.value.problem is Problem.CONFLICT

    def test_not_during_enumeration(self, hub: Hub, relay: NodeCore) -> None:
        """The model is about to be replaced (R5)."""
        hub.api.enumerate("kitchen-relay")
        with pytest.raises(ApiError) as error:
            hub.api.read_all("kitchen-relay")
        assert error.value.problem is Problem.CONFLICT

    def test_an_endpoint_it_does_not_have(self, hub: Hub, relay: NodeCore) -> None:
        with pytest.raises(ApiError) as error:
            hub.api.read_all("kitchen-relay", endpoint=9)
        assert error.value.problem is Problem.NOT_FOUND

    def test_a_node_without_an_address(self, hub: Hub) -> None:
        with pytest.raises(ApiError) as error:
            hub.api.read_all("kitchen-relay")
        assert error.value.problem is Problem.UNAVAILABLE

    def test_the_end_is_an_event(self, hub: Hub, relay: NodeCore) -> None:
        with hub.inst.core.events.subscribe() as events:
            hub.api.read_all("kitchen-relay")
            hub.inst.run(3.0)
            seen = list(iter(lambda: events.get(0), None))
        (done,) = [e for e in seen if e.name == "READOUT_DONE"]
        assert done.uid == Uid(RELAY_UID)
        assert " read" in done.text


class TestHealth:
    def test_a_binding_to_an_addressless_node_is_visible(self, hub: Hub) -> None:
        """A binding without a target **does nothing** — and it is better for that to be
        visible."""
        (binding,) = hub.api.bindings()
        assert binding.source_alias == "kitchen-switch"
        assert [t.alias for t in binding.targets] == ["kitchen-relay"]
        assert binding.targets[0].active is False

    def test_a_binding_comes_alive_once_the_target_joins(self, hub: Hub, relay: NodeCore) -> None:
        (binding,) = hub.api.bindings()
        assert binding.targets[0].active is True

    def test_health_gathers_counters_from_the_whole_hub(self, hub: Hub, relay: NodeCore) -> None:
        health = hub.api.health()
        assert health.nodes == 1 and health.ready == 1
        assert health.configured == 2, "two nodes in the file, one on the bus"
        assert [b.name for b in health.buses] == ["hub"]
        assert health.transactions["answered"] > 0
        assert health.reconcile["checked"] > 0
        assert health.reconcile["failed"] == 0


class TestAttributeWrite:
    """`config_set_attribute` — the write that **adds `type` when it is missing**.

    The loader requires an endpoint to give a type before it gets an attribute: without
    that there is no way to check whether `HoldThreshold` exists on that type at all. On a
    fresh configuration the first write therefore ended in an error that sounded like a
    hub fault and was one missing line.
    """

    def test_it_adds_the_type_when_the_endpoint_is_absent_from_the_file(
        self, clock: Clock, tmp_path: Path, relay: NodeCore
    ) -> None:
        clean = Hub(clock, tmp_path / "clean", EMPTY)
        joined(clean.inst, relay1(uid=RELAY_UID, address=0, clock=clock, name="r2"))
        clean.api.config_set_attribute(
            RELAY_UID.hex(), 4, "AutoOffTime", 30, etag=clean.api.config().etag
        )

        content = (clean.path).read_text(encoding="utf-8")
        assert "type: Output" in content, f"the type was not added:\n{content}"
        assert "AutoOffTime: 30" in content

    def test_it_does_not_add_the_type_when_it_is_already_there(
        self, hub: Hub, relay: NodeCore
    ) -> None:
        before = hub.path.read_text(encoding="utf-8").count("type: Output")
        hub.api.config_set_attribute(
            "kitchen-relay", 4, "AutoOffTime", 30, etag=hub.api.config().etag
        )
        assert hub.path.read_text(encoding="utf-8").count("type: Output") == before

    def test_state_is_not_configuration(self, hub: Hub, relay: NodeCore) -> None:
        with pytest.raises(ApiError) as error:
            hub.api.config_set_attribute(
                "kitchen-relay", 4, "OnOff", True, etag=hub.api.config().etag
            )
        assert error.value.problem is Problem.INVALID
        assert "is state" in error.value.message


class TestOnlyWhatTheEndpointDeclares:
    """The hub configures only what an endpoint declared (01-model-danych.md §2.1,
    `LB-HUB-026`) — and says so **before** the file or the bus, not after a refusal.

    `key1` has `HOLD` and `MULTICLICK`, no `RAW`, and exposes `HoldThreshold` but sets
    its other timings itself (zadania/zrobione/CR-01.md, N10).
    """

    @pytest.fixture
    def switch(self, hub: Hub, clock: Clock) -> NodeCore:
        return hub.join(key1(uid=SWITCH_UID, address=0, clock=clock, name="sw"))

    def test_a_value_of_an_undeclared_feature_never_reaches_the_file(
        self, hub: Hub, switch: NodeCore
    ) -> None:
        before = hub.path.read_text(encoding="utf-8")
        with pytest.raises(ApiError) as error:
            hub.api.config_set_attribute(
                "kitchen-switch", 4, "EventMode", "RAW", etag=hub.api.config().etag
            )
        assert error.value.problem is Problem.INVALID
        assert "feature" in error.value.message
        assert hub.path.read_text(encoding="utf-8") == before

    def test_nor_the_bus(self, hub: Hub, switch: NodeCore) -> None:
        before = hub.api.health().transactions["opened"]
        with pytest.raises(ApiError):
            hub.api.write("kitchen-switch", 4, "EventMode", "RAW")
        assert hub.api.health().transactions["opened"] == before

    def test_a_value_of_a_declared_feature_goes_through(self, hub: Hub, switch: NodeCore) -> None:
        hub.api.write("kitchen-switch", 4, "EventMode", "MULTICLICK")
        assert switch.endpoint(4, KeyInput).num(ki.EVENT_MODE) == ki.EventMode.MULTICLICK

    def test_an_attribute_the_node_turned_down_is_refused_from_then_on(
        self, hub: Hub, switch: NodeCore
    ) -> None:
        """The first write is let through — the hub cannot know yet. Reconciling finds
        out, and from then on the API answers by itself."""
        hub.api.config_set_attribute(
            "kitchen-switch", 4, "HoldTickInterval", 300, etag=hub.api.config().etag
        )
        hub.inst.run(1.0, step=0.02)
        assert any("does not expose HoldTickInterval" in i for i in hub.api.issues())

        with pytest.raises(ApiError) as error:
            hub.api.config_set_attribute(
                "kitchen-switch", 4, "HoldTickInterval", 400, etag=hub.api.config().etag
            )
        assert error.value.problem is Problem.INVALID

        ep4 = next(e for e in hub.api.node("kitchen-switch").detail if e.number == 4)
        tick = next(a for a in ep4.attributes if a.name == "HoldTickInterval")
        assert tick.available is False
        assert tick.configurable is False
        threshold = next(a for a in ep4.attributes if a.name == "HoldThreshold")
        assert threshold.available is True, "what the node exposes stays configurable"


class TestBusTraffic:
    """`/frames` — the same one sees with an analyser, only with names."""

    def test_frames_carry_direction_kind_and_reply_time(self, hub: Hub, relay: NodeCore) -> None:
        collected = self._collect(hub, lambda: hub.api.command("kitchen-relay", 4, "Toggle"))
        command = next(r for r in collected if r.kind == "COMMAND")
        response = next(r for r in collected if r.kind == "RESPONSE")

        assert command.outgoing is True and response.outgoing is False
        assert command.tid == response.tid
        assert response.result == "OK"
        assert response.elapsed is not None and response.elapsed > 0

    def test_a_reply_is_named_after_the_REQUEST_not_its_own_kind(
        self, hub: Hub, relay: NodeCore
    ) -> None:
        """`0x02` is the `Toggle` command or the `Red` attribute — depending on what was
        asked.

        The reply alone does not carry that, so the hub has to look into the open
        transaction. Without it the feed showed "a reply about Red" after a `Toggle` went
        out.
        """
        collected = self._collect(hub, lambda: hub.api.command("kitchen-relay", 4, "Toggle"))
        response = next(r for r in collected if r.kind == "RESPONSE")
        assert response.name == "Toggle"
        assert response.element == out.TOGGLE

    def test_time_is_counted_from_the_hub_start(self, hub: Hub, relay: NodeCore) -> None:
        """`61.204` reads, `409712.969` does not."""
        collected = self._collect(hub, lambda: hub.api.command("kitchen-relay", 4, "Toggle"))
        assert all(0 <= r.at < 3600 for r in collected)

    def test_nobody_listening_means_nothing_happens(self, hub: Hub, relay: NodeCore) -> None:
        """Unfolding names costs, and most of the time nobody is looking."""
        assert hub.inst.core.traffic.published == 0
        with hub.api.frames():
            hub.api.command("kitchen-relay", 4, "Toggle")
        assert hub.inst.core.traffic.published > 0

    @staticmethod
    def _collect(hub: Hub, co: Any) -> list[Any]:
        with hub.api.frames() as traffic:
            co()
            hub.inst.run(0.2, step=0.01)
            collected = []
            while (item := traffic.get(timeout=0.0)) is not None:
                collected.append(item)
        return collected


class TestPromises:
    def test_the_views_pass_through_json_without_a_translator(
        self, hub: Hub, relay: NodeCore
    ) -> None:
        """If a `Uid` left here as `bytes`, every adapter would have to know it — and two
        out of three would forget."""
        views: tuple[Any, ...] = (
            hub.api.node("kitchen-relay"),
            hub.api.read_all("kitchen-relay"),
            hub.api.health(),
            hub.api.commissioning(),
            *hub.api.bindings(),
            *hub.api.nodes(),
        )
        for view in views:
            json.dumps(asdict(view))

    def test_operations_return_data_not_strings(self, hub: Hub, relay: NodeCore) -> None:
        """An adapter is to get numbers and structure, not a formatted table row —
        otherwise the full-screen view would have to parse the CLI's output."""
        ep4 = next(e for e in hub.api.node("kitchen-relay").detail if e.number == 4)
        purpose = next(a for a in ep4.attributes if a.name == "Purpose")
        assert purpose.id == out.PURPOSE
        assert purpose.value == out.Purpose.LIGHT, "the value is a number…"
        assert purpose.label == "LIGHT", "…and the name a separate field, not glued on"

    def test_a_read_goes_through_the_core_queue_too(self, hub: Hub) -> None:
        """If `nodes()` read the model directly, this test would pass without pumping."""
        api = HubApi(hub.inst.core, timeout=0.05, wait=_without_pumping)
        with pytest.raises(ApiError) as error:
            api.nodes()
        assert error.value.problem is Problem.TIMEOUT


def _without_pumping(future: Future[Any], timeout: float) -> Any:
    """Nobody turns the CORE loop, so nothing will be settled."""
    del timeout
    if not future.done():
        raise FutureTimeout
    return future.result(timeout=0)


class TestBuses:
    """The hardware under each bus from the file (zadania/FEAT-01.md, step 5).

    The Rig's one open bus is named `hub`, so a file naming channel `hub` is the open
    one, and `can1` stands for a bus the hub left out at start.
    """

    BUSES = """
buses:
  - { name: ground-floor, kind: socketcan, channel: hub, bitrate: 250000 }
  - { name: first-floor, channel: can1 }
nodes: {}
bindings: []
"""

    @staticmethod
    def api(hub: Hub, drivers: dict[str, FakeDriver]) -> HubApi:
        hub.path.write_text(TestBuses.BUSES, encoding="utf-8")
        config = load(hub.path)
        hub.inst.core.submit(lambda: hub.inst.core.apply_config(config))
        hub.inst.run(0.02, step=0.005)
        return HubApi(
            hub.inst.core,
            timeout=2.0,
            wait=hub._wait,
            drivers=lambda bus: drivers[bus.channel],
        )

    def test_every_bus_from_the_file_is_listed(self, hub: Hub, relay: NodeCore) -> None:
        api = self.api(hub, {"hub": FakeDriver(), "can1": FakeDriver()})
        ground, first = api.buses()
        assert (ground.name, ground.kind, ground.channel, ground.bitrate) == (
            "ground-floor",
            "socketcan",
            "hub",
            250_000,
        )
        assert (ground.open, ground.nodes, ground.state) == (True, 1, "ok")
        assert (ground.hardware, ground.where) == ("mcp251xfd", "spi0.0 · can0")
        assert (first.open, first.nodes) == (False, 0), "left out at start: not open"

    def test_a_drift_is_shown_and_not_fixed(self, hub: Hub) -> None:
        """Looking changes nothing — a fix takes the bus down, so it is asked for."""
        driver = FakeDriver([BITRATE, LINK])
        api = self.api(hub, {"hub": driver, "can1": FakeDriver()})
        view = api.bus("socketcan", "ground-floor")
        assert view.state == "drift"
        assert [(c.what, c.how, c.manual) for c in view.changes] == [
            ("bitrate", BITRATE.how, False),
            ("link", LINK.how, False),
        ]
        assert "apply" not in driver.calls

    def test_what_only_a_person_can_do(self, hub: Hub) -> None:
        api = self.api(hub, {"hub": FakeDriver([MISSING]), "can1": FakeDriver()})
        assert api.bus("socketcan", "ground-floor").state == "manual"

    def test_a_driver_error_is_a_state_not_a_failure(self, hub: Hub) -> None:
        """A list of buses must not fail because one adapter is unplugged."""
        broken = FakeDriver(fails_plan=Unreachable("no /sys/class/net: this is not Linux"))
        api = self.api(hub, {"hub": FakeDriver(), "can1": broken})
        _, first = api.buses()
        assert (first.state, first.error) == ("error", "no /sys/class/net: this is not Linux")

    def test_apply_carries_out_the_automatic_changes(self, hub: Hub) -> None:
        driver = FakeDriver([BITRATE, MISSING])
        api = self.api(hub, {"hub": driver, "can1": FakeDriver()})
        view = api.bus_apply("socketcan", "ground-floor")
        assert driver.applied == [BITRATE], "the manual one stays for a person"
        assert driver.desired == SocketCanSettings(bitrate=250_000)
        assert (view.state, [c.what for c in view.changes]) == ("manual", ["interface"])

    def test_apply_refused_by_sudo(self, hub: Hub) -> None:
        refused = Rejected("ip link set hub down", "sudo: a password is required")
        api = self.api(
            hub, {"hub": FakeDriver([BITRATE], fails_apply=refused), "can1": FakeDriver()}
        )
        with pytest.raises(ApiError) as error:
            api.bus_apply("socketcan", "ground-floor")
        assert error.value.problem is Problem.HARDWARE
        assert "NOPASSWD" in error.value.message

    def test_an_unknown_bus(self, hub: Hub) -> None:
        api = self.api(hub, {"hub": FakeDriver(), "can1": FakeDriver()})
        for call in (api.bus, api.bus_apply):
            with pytest.raises(ApiError) as error:
                call("socketcan", "attic")
            assert error.value.problem is Problem.NOT_FOUND

    def test_a_bus_is_found_under_its_own_kind_only(self, hub: Hub) -> None:
        """Each kind has its own contract; a SocketCAN bus is not reachable as another."""
        api = self.api(hub, {"hub": FakeDriver(), "can1": FakeDriver()})
        with pytest.raises(ApiError) as error:
            api.bus("usr-can315", "ground-floor")
        assert error.value.problem is Problem.NOT_FOUND

    def test_the_view_goes_through_json(self, hub: Hub) -> None:
        api = self.api(hub, {"hub": FakeDriver([BITRATE]), "can1": FakeDriver()})
        for view in api.buses():
            json.dumps(asdict(view))
