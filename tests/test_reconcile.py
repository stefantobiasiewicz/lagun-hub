"""Reconciling the configuration with the nodes, and bindings on an attribute change.

> **The hub writes to a node what it has at its own end — and that way round, never the
> other.**
"""

from __future__ import annotations

from pathlib import Path

from lagunhub.core.config import load
from lagunhub.core.config.apply import apply
from lagunhub.core.model import NodeModel, Uid
from lagunhub.protocol.endpoints import key_input as ki
from lagunhub.protocol.endpoints import output as out
from lagunhub.protocol.registry import CommonAttribute, MessageKind, NodeClass
from tests.conftest import Clock
from tests.node.devices import key1, panel3, relay1
from tests.node.endpoints.key_input import KeyInput
from tests.node.endpoints.output import Output
from tests.node.node import NodeCore
from tests.test_enumeration import Rig, joined

SWITCH = Uid(bytes(range(8)))
RELAY = Uid(bytes(range(8, 16)))
PANEL = Uid(bytes(range(16, 24)))

CONFIG = """
buses: [vcan0]
groups:
  1: { name: all-lights }
nodes:
  "0001020304050607":
    alias: kitchen-switch
    name: "The kitchen key"
    endpoints:
      4: { type: KeyInput, EventMode: MULTICLICK, HoldThreshold: 600 }
  "08090a0b0c0d0e0f":
    alias: kitchen-relay
    name: "The lamp over the table"
    endpoints:
      4: { type: Output, PowerOnBehaviour: RESTORE, groups: [all-lights] }
bindings:
  - from: { node: kitchen-switch, endpoint: 4, type: KeyInput, event: Click }
    to:   { node: kitchen-relay,  endpoint: 4, type: Output,   command: Toggle }
"""


def build(clock: Clock, tmp_path: Path, content: str = CONFIG) -> Rig:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    inst = Rig(clock)
    apply(inst.installation, load(path))
    return inst


class TestReconciling:
    def test_it_writes_only_the_differences(self, clock: Clock, tmp_path: Path) -> None:
        inst = build(clock, tmp_path)
        joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock, name="sw"))

        rec = inst.core.reconcile
        assert rec.checked == 2, "two configured attributes"
        assert rec.mismatched == 2, "both differed from the defaults"
        assert rec.written == 2
        assert rec.failed == 0

    def test_a_common_attribute_can_be_configured_too(self, clock: Clock, tmp_path: Path) -> None:
        """`ReportInterval` and company are common attributes, not a type's — and are
        adjusted more often than any other.

        Before the hub API they could not be configured at all: the file rejected the name,
        because `TypeDef` did not know them, and reconciling quietly skipped the number.
        """
        common_ = CONFIG.replace(
            "4: { type: KeyInput, EventMode: MULTICLICK, HoldThreshold: 600 }",
            "4: { type: KeyInput, EventMode: MULTICLICK, ReportInterval: 60, Enabled: true }",
        )
        inst = build(clock, tmp_path, common_)
        node = joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock, name="sw"))

        key = node.endpoint(4, KeyInput)
        assert key.values[CommonAttribute.ReportInterval] == 60
        assert inst.core.reconcile.failed == 0

    def test_the_value_reaches_the_node(self, clock: Clock, tmp_path: Path) -> None:
        inst = build(clock, tmp_path)
        node = joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock, name="sw"))

        key = node.endpoint(4, KeyInput)
        assert key.num(ki.EVENT_MODE) == ki.EventMode.MULTICLICK
        assert key.num(ki.HOLD_THRESHOLD) == 600

    def test_what_already_agrees_is_not_rewritten(self, clock: Clock, tmp_path: Path) -> None:
        """The hub writes to a node only when the picture has drifted."""
        content = CONFIG.replace("EventMode: MULTICLICK, HoldThreshold: 600", "HoldThreshold: 500")
        inst = build(clock, tmp_path, content)
        joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock, name="sw"))

        assert inst.core.reconcile.checked == 1
        assert inst.core.reconcile.mismatched == 0
        assert inst.core.reconcile.written == 0

    def test_a_node_without_configuration_is_left_alone(self, clock: Clock, tmp_path: Path) -> None:
        inst = build(clock, tmp_path)
        joined(inst, panel3(uid=bytes(range(16, 24)), address=0, clock=clock, name="p"))
        assert inst.core.reconcile.checked == 0

    def test_a_type_differing_from_the_descriptor_lands_on_the_issue_list(
        self, clock: Clock, tmp_path: Path
    ) -> None:
        """The second phase of validation — against a real installation
        (KONFIGURACJA.md §10)."""
        content = CONFIG.replace(
            "4: { type: KeyInput, EventMode: MULTICLICK, HoldThreshold: 600 }",
            "4: { type: Output, Purpose: LIGHT }",
        )
        inst = build(clock, tmp_path, content)
        joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock, name="sw"))

        assert any("is of type KeyInput" in u for u in inst.installation.issues)
        assert inst.core.reconcile.written == 0

    def test_an_endpoint_the_node_does_not_have(self, clock: Clock, tmp_path: Path) -> None:
        content = CONFIG.replace(
            "      4: { type: KeyInput, EventMode: MULTICLICK, HoldThreshold: 600 }",
            "      4: { type: KeyInput, HoldThreshold: 600 }\n"
            "      9: { type: KeyInput, HoldThreshold: 600 }",
        )
        inst = build(clock, tmp_path, content)
        joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock, name="sw"))

        assert any("ep9" in u and "absent from the node" in u for u in inst.installation.issues)

    def test_a_silent_node_ends_in_an_issue(self, clock: Clock, tmp_path: Path) -> None:
        inst = build(clock, tmp_path)
        node = key1(uid=bytes(range(8)), address=0, clock=clock, name="sw")
        inst.core.commissioning.accept_new()
        inst.add(node)
        inst.run(1.85, step=0.05)
        node.faults.silent_until = clock.now + 60.0
        inst.run(2.0, step=0.05)

        assert inst.core.reconcile.failed >= 1 or inst.core.enumeration.failed >= 1


class TestOnlyWhatTheEndpointDeclares:
    """Reconciling sends nothing an endpoint cannot have — the file is wrong, not the
    node (01-model-danych.md §2.1, `LB-HUB-026`; zadania/zrobione/CR-01.md, N10)."""

    def test_a_value_of_an_undeclared_feature_is_not_written(
        self, clock: Clock, tmp_path: Path
    ) -> None:
        content = CONFIG.replace("EventMode: MULTICLICK", "EventMode: RAW")
        inst = build(clock, tmp_path, content)
        node = joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock, name="sw"))

        assert any("EventMode = 2 needs a feature" in i for i in inst.installation.issues)
        assert node.endpoint(4, KeyInput).num(ki.EVENT_MODE) == ki.EventMode.CLICK
        assert inst.core.reconcile.failed == 0

    def test_an_attribute_of_an_undeclared_feature_is_not_sent(
        self, clock: Clock, tmp_path: Path
    ) -> None:
        """A doorbell: no features, so not a single attribute to configure."""
        content = CONFIG.replace("EventMode: MULTICLICK, HoldThreshold: 600", "HoldEnabled: false")
        inst = build(clock, tmp_path, content)
        bell = NodeCore(
            uid=bytes(range(8)),
            device_type=0x01F0,
            node_class=NodeClass.CONTROL,
            functional={4: KeyInput(channel=1)},
            address=0,
            clock=clock,
            name="bell",
        )
        joined(inst, bell)

        assert any("HoldEnabled needs a feature" in i for i in inst.installation.issues)
        assert inst.core.reconcile.checked == 0, "not even a read went out"

    def test_an_optional_attribute_the_node_lacks_is_learned_once(
        self, clock: Clock, tmp_path: Path
    ) -> None:
        content = CONFIG.replace("EventMode: MULTICLICK", "HoldTickInterval: 300")
        inst = build(clock, tmp_path, content)
        joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock, name="sw"))

        issues = [i for i in inst.installation.issues if "HoldTickInterval" in i]
        assert issues and "does not expose" in issues[0]
        assert inst.core.reconcile.failed == 0, "a device setting it itself is no failure"
        model = inst.installation.nodes[SWITCH]
        assert ki.HOLD_TICK_INTERVAL in model.endpoints[4].absent

        opened = inst.core.transactions.opened
        inst.core.reconcile.start(model)
        inst.run(0.5, step=0.02)
        # HoldThreshold is read again to check agreement; HoldTickInterval is not asked.
        assert inst.core.transactions.opened == opened + 1


class TestBindingOnAttributeChange:
    """07-hub.md §3.1 — a command fires on a **change of value**, not on every report; a
    write fires on every report (zadania/zrobione/FEAT-13.md N8)."""

    TRESC = CONFIG.replace(
        "from: { node: kitchen-switch, endpoint: 4, type: KeyInput, event: Click }",
        "from: { node: kitchen-relay, endpoint: 4, type: Output, attribute: OnOff }",
    ).replace(
        "to:   { node: kitchen-relay,  endpoint: 4, type: Output,   command: Toggle }",
        "to:   { node: kitchen-switch, endpoint: 4, type: KeyInput, attribute: HoldEnabled }",
    )

    def test_a_change_fires_it(self, clock: Clock, tmp_path: Path) -> None:
        inst = build(clock, tmp_path, self.TRESC)
        relay = joined(inst, relay1(uid=bytes(range(8, 16)), address=0, clock=clock, name="r"))
        joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock, name="sw"))
        before = inst.core.transactions.opened

        relay.endpoint(4, Output).set(out.ON_OFF, True)
        inst.run(0.3, step=0.02)

        assert inst.core.transactions.opened > before, "the change of state pulled a write along"

    COMMAND = TRESC.replace(
        "to:   { node: kitchen-switch, endpoint: 4, type: KeyInput, attribute: HoldEnabled }",
        "to:   { node: kitchen-relay,  endpoint: 4, type: Output,   command: On }",
    )

    def test_the_same_report_twice_does_not_fire_a_command(
        self, clock: Clock, tmp_path: Path
    ) -> None:
        """Otherwise the periodic refresh would generate commands in circles."""
        inst = build(clock, tmp_path, self.COMMAND)
        relay = joined(inst, relay1(uid=bytes(range(8, 16)), address=0, clock=clock, name="r"))
        joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock, name="sw"))

        output = relay.endpoint(4, Output)
        output.set(out.ON_OFF, True)
        inst.run(0.3, step=0.02)
        after_first = inst.core.transactions.opened

        output.report(out.ON_OFF)  # a periodic report of an UNCHANGED value
        relay.flush()
        inst.run(0.3, step=0.02)

        assert inst.core.transactions.opened == after_first

    def test_the_same_report_twice_writes_twice(self, clock: Clock, tmp_path: Path) -> None:
        """A write target may expire on its node (`Display.ContentTimeout`); the periodic
        report is what refreshes it."""
        inst = build(clock, tmp_path, self.TRESC)
        relay = joined(inst, relay1(uid=bytes(range(8, 16)), address=0, clock=clock, name="r"))
        joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock, name="sw"))

        output = relay.endpoint(4, Output)
        output.set(out.ON_OFF, True)
        inst.run(0.3, step=0.02)
        after_first = inst.core.transactions.opened

        output.report(out.ON_OFF)
        relay.flush()
        inst.run(0.3, step=0.02)

        assert inst.core.transactions.opened == after_first + 1

    def test_a_report_does_not_enter_the_fast_path(self, clock: Clock, tmp_path: Path) -> None:
        inst = build(clock, tmp_path, self.TRESC)
        joined(inst, relay1(uid=bytes(range(8, 16)), address=0, clock=clock, name="r"))
        joined(inst, key1(uid=bytes(range(8)), address=0, clock=clock, name="sw"))

        snapshot = inst.installation.snapshot()
        assert snapshot.bind == {}
        assert (RELAY, 4, out.ON_OFF) in snapshot.on_report


def test_three_bindings_from_the_spec_through_one_engine(clock: Clock, tmp_path: Path) -> None:
    """07-hub.md §3.3 — the pattern is the same in each, and the hub does not tell them
    apart."""
    content = """
buses: [vcan0]
groups:
  1: { name: all-lights }
nodes:
  "0001020304050607": { alias: sw }
  "08090a0b0c0d0e0f":
    alias: relay
    endpoints:
      4: { type: Output, groups: [all-lights] }
bindings:
  - from: { node: sw, endpoint: 4, type: KeyInput, event: Click }
    to:   { node: relay, endpoint: 4, type: Output, command: Toggle }
  - from: { node: relay, endpoint: 4, type: Output, attribute: OnOff }
    to:   { node: sw, endpoint: 4, type: KeyInput, attribute: HoldEnabled }
  - from: { node: sw, endpoint: 5, type: KeyInput, event: Click }
    to:   { group: all-lights, endpoint: 0, type: Output, command: Off }
"""
    inst = build(clock, tmp_path, content)
    inst.installation.add(NodeModel(uid=SWITCH, address=0x02A, bus=0))
    inst.installation.add(NodeModel(uid=RELAY, address=0x040, bus=0))
    snapshot = inst.installation.snapshot()

    kinds = {
        MessageKind(t.can_id >> 25).name
        for targets in (*snapshot.bind.values(), *snapshot.on_report.values())
        for t in targets
    }
    assert kinds == {"COMMAND", "WRITE", "GROUP_COMMAND"}
