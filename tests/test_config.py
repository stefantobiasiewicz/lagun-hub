"""The configuration: loading, static validation, a target in three shapes."""

from __future__ import annotations

from pathlib import Path

import pytest

from lagunhub.core.config import ConfigError, load
from lagunhub.core.config.apply import apply
from lagunhub.core.model import Installation, NodeModel, NodeState, Uid
from lagunhub.protocol.endpoints import key_input as ki
from lagunhub.protocol.endpoints import output as out
from lagunhub.protocol.frame import Frame
from lagunhub.protocol.registry import EndpointType, MessageKind

SWITCH = Uid(bytes(range(8)))
RELAY = Uid(bytes(range(8, 16)))

BASE = """
buses:
  - { name: ground-floor, channel: vcan0, bitrate: 250000 }
groups:
  1: { name: all-lights }
nodes:
  "0001020304050607":
    alias: kitchen-switch
    name: "The kitchen key"
    endpoints:
      4: { type: KeyInput, HoldTickInterval: 300, HoldThreshold: 600 }
  "08090a0b0c0d0e0f":
    alias: kitchen-relay
    name: "The lamp over the table"
    endpoints:
      4: { type: Output, Purpose: LIGHT, groups: [all-lights] }
bindings:
  - from: { node: kitchen-switch, endpoint: 4, type: KeyInput, event: Click }
    to:   { node: kitchen-relay,  endpoint: 4, type: Output,   command: Toggle }
"""


def save(tmp_path: Path, content: str = BASE) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def without(content: str, *co: str) -> str:
    for fragment in co:
        content = content.replace(fragment, "")
    return content


class TestLoading:
    def test_buses_and_groups(self, tmp_path: Path) -> None:
        config = load(save(tmp_path))
        assert config.channels == ("vcan0",)
        assert config.buses[0].name == "ground-floor"
        assert config.buses[0].bitrate == 250_000
        assert {n: g.name for n, g in config.groups.items()} == {1: "all-lights"}

    def test_an_alias_translates_to_a_uid(self, tmp_path: Path) -> None:
        """A Uid appears in the configuration exactly once (KONFIGURACJA.md §4)."""
        config = load(save(tmp_path))
        assert {n.alias: n.uid for n in config.nodes} == {
            "kitchen-switch": SWITCH,
            "kitchen-relay": RELAY,
        }

    def test_attributes_by_name_not_by_number(self, tmp_path: Path) -> None:
        config = load(save(tmp_path))
        switch = config.node(SWITCH)
        assert switch is not None
        assert switch.endpoints[4].attributes == {
            ki.HOLD_TICK_INTERVAL: 300,
            ki.HOLD_THRESHOLD: 600,
        }

    def test_an_enum_value_by_name(self, tmp_path: Path) -> None:
        config = load(save(tmp_path))
        relay = config.node(RELAY)
        assert relay is not None
        assert relay.endpoints[4].attributes == {out.PURPOSE: out.Purpose.LIGHT}

    def test_a_group_by_name(self, tmp_path: Path) -> None:
        config = load(save(tmp_path))
        relay = config.node(RELAY)
        assert relay is not None
        assert relay.endpoints[4].groups == (1,)

    def test_the_directory_merges_three_files(self, tmp_path: Path) -> None:
        (tmp_path / "hub.yaml").write_text("buses: [vcan0]\n", encoding="utf-8")
        (tmp_path / "nodes.yaml").write_text(
            'nodes:\n  "0001020304050607": { alias: a }\n', encoding="utf-8"
        )
        config = load(tmp_path)
        assert config.channels == ("vcan0",)
        assert config.nodes[0].alias == "a"

    def test_without_an_alias_the_uid_is_the_key(self, tmp_path: Path) -> None:
        """An alias is a label, not an identity — one is allowed not to give one."""
        content = BASE.replace("    alias: kitchen-relay\n", "")
        config = load(
            save(tmp_path, content.replace("node: kitchen-relay", "node: 08090a0b0c0d0e0f"))
        )
        relay = config.node(RELAY)
        assert relay is not None
        assert relay.alias == "08090a0b0c0d0e0f"

    def test_a_node_may_be_referred_to_by_uid(self, tmp_path: Path) -> None:
        content = BASE.replace(
            "node: kitchen-relay,  endpoint", "node: 08090a0b0c0d0e0f,  endpoint"
        )
        config = load(save(tmp_path, content))
        assert config.bindings[0].targets[0].uid == RELAY


class TestStaticValidation:
    """Stronger than it looks — the type catalogue knows what exists in a type."""

    def test_a_typo_in_a_command_name(self, tmp_path: Path) -> None:
        content = BASE.replace("command: Toggle", "command: Toogle")
        with pytest.raises(ValueError, match="Toggle"):
            load(save(tmp_path, content))

    def test_an_unknown_endpoint_type(self, tmp_path: Path) -> None:
        content = BASE.replace("type: Output,   command", "type: Dishwasher,   command")
        with pytest.raises(ConfigError, match="unknown endpoint type"):
            load(save(tmp_path, content))

    def test_an_attribute_that_is_state(self, tmp_path: Path) -> None:
        """`OnOff` is state, not configuration (KONFIGURACJA.md §7)."""
        content = BASE.replace("Purpose: LIGHT", "OnOff: true")
        with pytest.raises(ConfigError, match="is state"):
            load(save(tmp_path, content))

    def test_a_system_endpoint_cannot_be_disabled(self, tmp_path: Path) -> None:
        """The node would refuse it; in the file it would be reconciled forever
        (01-model-danych.md §4.1)."""
        content = BASE.replace(
            "      4: { type: KeyInput,",
            "      1: { type: Diagnostics, Enabled: false }\n      4: { type: KeyInput,",
        )
        with pytest.raises(ConfigError, match="system endpoint cannot be disabled"):
            load(save(tmp_path, content))

    def test_a_system_endpoint_takes_reporting_settings(self, tmp_path: Path) -> None:
        content = BASE.replace(
            "      4: { type: KeyInput,",
            "      1: { type: Diagnostics, ReportInterval: 60 }\n      4: { type: KeyInput,",
        )
        load(save(tmp_path, content))

    def test_an_attribute_out_of_range(self, tmp_path: Path) -> None:
        content = BASE.replace("HoldTickInterval: 300", "HoldTickInterval: 5000")
        with pytest.raises(ConfigError, match="is outside 50…1000"):
            load(save(tmp_path, content))

    def test_an_unknown_enum_value(self, tmp_path: Path) -> None:
        content = BASE.replace("Purpose: LIGHT", "Purpose: ZMYWARKA")
        with pytest.raises(ConfigError, match="ZMYWARKA"):
            load(save(tmp_path, content))

    def test_an_unknown_group(self, tmp_path: Path) -> None:
        content = BASE.replace("groups: [all-lights]", "groups: [cellar]")
        with pytest.raises(ConfigError, match="cellar"):
            load(save(tmp_path, content))

    def test_a_group_keyed_by_name_says_outright_what_is_wrong(self, tmp_path: Path) -> None:
        """The number travels in the frame, the name does not — so the number is the key."""
        content = BASE.replace("  1: { name: all-lights }", "  all-lights: 1")
        with pytest.raises(ConfigError, match="a group's key is its NUMBER"):
            load(save(tmp_path, content))

    def test_a_group_out_of_range(self, tmp_path: Path) -> None:
        content = BASE.replace("  1: { name: all-lights }", "  4096: { name: too-big }")
        with pytest.raises(ConfigError, match="is outside"):
            load(save(tmp_path, content))

    def test_a_group_may_be_referred_to_by_number(self, tmp_path: Path) -> None:
        content = BASE.replace("groups: [all-lights]", "groups: [1]")
        config = load(save(tmp_path, content))
        relay = config.node(RELAY)
        assert relay is not None
        assert relay.endpoints[4].groups == (1,)

    def test_a_binding_to_an_unknown_node(self, tmp_path: Path) -> None:
        content = BASE.replace("node: kitchen-relay,  endpoint", "node: garage,  endpoint")
        with pytest.raises(ConfigError, match="garage"):
            load(save(tmp_path, content))

    def test_an_alias_used_twice(self, tmp_path: Path) -> None:
        content = BASE.replace("alias: kitchen-relay", "alias: kitchen-switch")
        with pytest.raises(ConfigError, match="used twice"):
            load(save(tmp_path, content))

    def test_a_uid_of_the_wrong_length(self, tmp_path: Path) -> None:
        content = BASE.replace('"0001020304050607":', '"000102":')
        with pytest.raises(ConfigError, match="8-16"):
            load(save(tmp_path, content))

    def test_a_non_reporting_attribute_cannot_be_a_source(self, tmp_path: Path) -> None:
        """The hub would never learn about the change (07-hub.md §3.1)."""
        content = BASE.replace(
            "type: KeyInput, event: Click", "type: KeyInput, attribute: HoldTickInterval"
        )
        with pytest.raises(ConfigError, match="is not reported"):
            load(save(tmp_path, content))

    def test_a_read_only_attribute_cannot_be_a_write_target(self, tmp_path: Path) -> None:
        content = BASE.replace(
            "type: Output,   command: Toggle", "type: Output,   attribute: OnOff"
        )
        with pytest.raises(ConfigError, match="RO"):
            load(save(tmp_path, content))

    def test_an_attribute_without_an_endpoint_type(self, tmp_path: Path) -> None:
        content = BASE.replace("4: { type: KeyInput, HoldTickInterval", "4: { HoldTickInterval")
        with pytest.raises(ConfigError, match="`type`"):
            load(save(tmp_path, content))


class TestThreeShapesOfTarget:
    """07-hub.md §3.2 — and the hub need not tell them apart (§3.3)."""

    def setup(self, tmp_path: Path, content: str) -> Installation:
        inst = Installation()
        apply(inst, load(save(tmp_path, content)))
        inst.add(NodeModel(uid=SWITCH, address=0x02A, bus=0, state=NodeState.READY))
        inst.add(NodeModel(uid=RELAY, address=0x040, bus=1, state=NodeState.READY))
        return inst

    def test_an_addressed_command(self, tmp_path: Path) -> None:
        inst = self.setup(tmp_path, BASE)
        (target,) = inst.snapshot().bind[(SWITCH, 4, ki.CLICK)]

        fields = Frame(target.can_id).fields
        assert fields.kind == MessageKind.COMMAND
        assert (fields.address, fields.endpoint, fields.element) == (0x040, 4, out.TOGGLE)
        assert target.transactional
        assert target.bus == 1, "the target sits on the second bus and the hub knows it"

    def test_a_group_command_to_every_bus_with_members(self, tmp_path: Path) -> None:
        content = BASE.replace(
            "to:   { node: kitchen-relay,  endpoint: 4, type: Output,   command: Toggle }",
            "to:   { group: all-lights, endpoint: 0, type: Output, command: Off }",
        )
        inst = self.setup(tmp_path, content)
        targets = inst.snapshot().bind[(SWITCH, 4, ki.CLICK)]

        assert len(targets) == 1, "only the relay is in the group, so one bus"
        fields = Frame(targets[0].can_id).fields
        assert fields.kind == MessageKind.GROUP_COMMAND
        assert fields.address == 1, "the ADDRESS field carries the group number"
        assert fields.endpoint == 0, "zero means: hand it out to the group's members"
        assert not targets[0].transactional, "a group command has no Tid and no reply"
        assert targets[0].payload == b"", "Off has no arguments"

    def test_an_attribute_write(self, tmp_path: Path) -> None:
        """When the target is a value, not an action — that serves queries
        (07-hub.md §3.2)."""
        content = BASE.replace(
            "to:   { node: kitchen-relay,  endpoint: 4, type: Output,   command: Toggle }",
            "to:   { node: kitchen-relay,  endpoint: 4, type: Output,   attribute: AutoOffTime }",
        )
        inst = self.setup(tmp_path, content)
        (target,) = inst.snapshot().bind[(SWITCH, 4, ki.CLICK)]

        assert Frame(target.can_id).fields.kind == MessageKind.WRITE
        assert target.transactional

    def test_a_group_without_members_yields_zero_frames(self, tmp_path: Path) -> None:
        content = BASE.replace("groups: [all-lights]", "groups: []").replace(
            "to:   { node: kitchen-relay,  endpoint: 4, type: Output,   command: Toggle }",
            "to:   { group: all-lights, endpoint: 0, type: Output, command: Off }",
        )
        inst = self.setup(tmp_path, content)
        assert inst.snapshot().bind.get((SWITCH, 4, ki.CLICK)) is None

    def test_a_binding_on_attribute_change_is_not_a_fast_path(self, tmp_path: Path) -> None:
        content = BASE.replace(
            "from: { node: kitchen-switch, endpoint: 4, type: KeyInput, event: Click }",
            "from: { node: kitchen-relay, endpoint: 4, type: Output, attribute: OnOff }",
        )
        inst = self.setup(tmp_path, content)
        snapshot = inst.snapshot()

        assert snapshot.bind == {}, "a report has priority 0x8 and no time budget at all"
        assert (RELAY, 4, out.ON_OFF) in snapshot.on_report


def test_names_and_types_reach_the_model(tmp_path: Path) -> None:
    inst = Installation()
    apply(inst, load(save(tmp_path)))

    assert inst.snapshot().names[SWITCH] == "The kitchen key"
    assert inst.groups_of(RELAY) == (1,)
    wanted = inst.wanted[RELAY]
    assert wanted.endpoints[4].attributes == {out.PURPOSE: out.Purpose.LIGHT}
    assert wanted.endpoints[4].type == EndpointType.Output
