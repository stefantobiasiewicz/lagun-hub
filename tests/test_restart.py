"""A restart of the hub — the installation is still there afterwards
(zadania/zrobione/BUG-05.md; lagunbus-spec/07-hub.md §5).

Before, a restart looked to the installation exactly like a lost database: every node
the hub knew came back without an address, so nothing could be done from the view, and a
node that spoke up waited as `PENDING` for somebody to admit it again. Two things fix it:

* the address, bus and `ShortId` of a joined node are written to its entry in the file
  (`joined`), and a hub starting on that file talks to the node at once — no frame from
  it needed, no polling (D1, D3);
* a node under an address the hub does not know, whose `Uid` is in the file, is
  enumerated without asking — the entry is the operator's decision (D4).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lagunhub.api import ApiError, Problem
from lagunhub.core.config import load
from lagunhub.core.config.apply import apply, bus_index
from lagunhub.core.config.loader import ConfigError, from_document
from lagunhub.core.config.store import ConfigStore
from lagunhub.core.model import Installation, NodeState, Uid
from lagunhub.protocol.endpoints import key_input as ki
from lagunhub.protocol.endpoints.output import ON_OFF, TOGGLE
from lagunhub.protocol.frame import encode_id
from lagunhub.protocol.registry import MessageKind
from tests.conftest import Clock
from tests.node.devices import relay1
from tests.node.endpoints.device import short_id
from tests.node.endpoints.output import Output
from tests.node.node import NodeCore
from tests.test_api import CONFIG, RELAY_UID, SWITCH_UID, Hub

RELAY = Uid(RELAY_UID)
SWITCH = Uid(SWITCH_UID)


def _restart(clock: Clock, before: Hub, where: Path) -> Hub:
    """A new hub on the file the old one left — what `lagun-hub run` after `Ctrl-C` is."""
    return Hub(clock, where, before.path.read_text(encoding="utf-8"))


def _back(hub: Hub, clock: Clock, address: int) -> NodeCore:
    """The relay, still at its address on the bus (it is in its NVS), whose `Booted` went
    out before the hub was listening — the usual order on a Raspberry Pi."""
    node = hub.inst.add(relay1(uid=RELAY_UID, address=address, clock=clock, name="relay"))
    while hub.inst.hub_port.recv() is not None:
        pass
    return node


@pytest.fixture
def joined(clock: Clock, tmp_path: Path) -> tuple[Hub, int]:
    """A hub that admitted the relay — and the address it got."""
    hub = Hub(clock, tmp_path / "before")
    relay = hub.join(relay1(uid=RELAY_UID, address=0, clock=clock, name="relay"))
    return hub, relay.address


class TestWritten:
    def test_joining_writes_where_the_node_sits(self, joined: tuple[Hub, int]) -> None:
        hub, address = joined
        entry = load(hub.path).node(RELAY)

        assert entry is not None and entry.joined is not None
        assert entry.joined.address == address
        assert entry.joined.short_id == short_id(RELAY_UID)
        assert entry.joined.bus == "hub", "the open bus's name — the file names no such bus"

    def test_in_hexadecimal_on_one_line(self, joined: tuple[Hub, int]) -> None:
        hub, address = joined
        text = hub.path.read_text(encoding="utf-8")

        assert f"joined: {{address: 0x{address:03X}, bus: hub, short_id: 0x" in text

    def test_the_rest_of_the_entry_is_untouched(self, joined: tuple[Hub, int]) -> None:
        hub, _ = joined
        entry = load(hub.path).node(RELAY)

        assert entry is not None
        assert entry.alias == "kitchen-relay"
        assert entry.name == "The lamp over the table"
        assert entry.endpoints[4].groups == (1,)

    def test_enumerating_again_writes_nothing(self, joined: tuple[Hub, int]) -> None:
        """The file changes only when the bus says something new — an operator's `E`
        is no reason to touch somebody's file."""
        hub, _ = joined
        before = hub.path.read_text(encoding="utf-8")

        hub.api.enumerate("kitchen-relay")
        hub.inst.run(1.0)

        assert hub.path.read_text(encoding="utf-8") == before

    def test_a_node_without_an_entry_gets_one(self, clock: Clock, tmp_path: Path) -> None:
        """Admitted and not named yet is a normal state — and it has to survive a restart
        too."""
        hub = Hub(clock, tmp_path, "buses: [vcan0]\nnodes: {}\nbindings: []\n")
        hub.join(relay1(uid=RELAY_UID, address=0, clock=clock, name="relay"))

        entry = load(hub.path).node(RELAY)
        assert entry is not None and entry.joined is not None

    def test_a_hand_edit_not_picked_up_yet_goes_in_with_it(self, joined: tuple[Hub, int]) -> None:
        """The write marks the file as seen — so the edit it carried along must go in
        the way any edit does, not be left for a reload that will never come."""
        hub, _ = joined
        text = hub.path.read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if "joined:" not in line]
        hub.path.write_text(
            "\n".join(lines).replace("room: kitchen", "room: hall", 1) + "\n", encoding="utf-8"
        )

        hub.inst.core.note_joined(hub.inst.installation.nodes[RELAY])

        config = hub.inst.core.config
        assert config is not None
        assert {entry.room for entry in config.nodes} == {"hall", "kitchen"}
        assert "joined:" in hub.path.read_text(encoding="utf-8")


class TestAfterRestart:
    def test_the_node_is_ready_without_a_frame(
        self, clock: Clock, tmp_path: Path, joined: tuple[Hub, int]
    ) -> None:
        """Not "not heard since the start", not polled: in the file, so joined (D3)."""
        hub = _restart(clock, joined[0], tmp_path / "after")

        view = next(n for n in hub.api.nodes() if n.alias == "kitchen-relay")
        assert view.state == "READY"
        assert view.address == joined[1]
        assert hub.inst.installation.addresses.taken(joined[1]), "nobody else gets it"

    def test_a_binding_targets_it_from_the_first_second(
        self, clock: Clock, tmp_path: Path, joined: tuple[Hub, int]
    ) -> None:
        """The fast path needs the address and nothing else — not the descriptor."""
        hub = _restart(clock, joined[0], tmp_path / "after")

        targets = hub.inst.installation.snapshot().bind[(SWITCH, 4, ki.CLICK)]

        assert [t.can_id for t in targets] == [encode_id(MessageKind.COMMAND, joined[1], 4, TOGGLE)]

    def test_a_command_works_before_the_node_says_anything(
        self, clock: Clock, tmp_path: Path, joined: tuple[Hub, int]
    ) -> None:
        """The case from the Raspberry Pi: restart, and `the hub has not seen it yet`.
        The descriptor is read first — one node, as part of the command (N9)."""
        hub = _restart(clock, joined[0], tmp_path / "after")
        relay = _back(hub, clock, joined[1])
        assert not hub.inst.installation.nodes[RELAY].described

        outcome = hub.api.command("kitchen-relay", 4, "Toggle")

        assert outcome.result == "OK"
        assert relay.endpoint(4, Output).values[ON_OFF] is True
        assert hub.inst.installation.nodes[RELAY].described

    def test_reading_everything_works_too(
        self, clock: Clock, tmp_path: Path, joined: tuple[Hub, int]
    ) -> None:
        """`G` in the view, straight after a restart."""
        hub = _restart(clock, joined[0], tmp_path / "after")
        _back(hub, clock, joined[1])

        view = hub.api.read_all("kitchen-relay")

        assert view.started and view.pending > 0

    def test_the_view_reads_what_it_is_when_asked_to(
        self, clock: Clock, tmp_path: Path, joined: tuple[Hub, int]
    ) -> None:
        """The binding wizard straight after a restart: "the dial has no endpoints" until
        somebody turned it (zadania/BUG-09.md). Asked with `describe`, the view reads the
        descriptor first, as a command does; asked without, it does not — refreshing the
        view must never wait on a silent node."""
        hub = _restart(clock, joined[0], tmp_path / "after")
        _back(hub, clock, joined[1])

        plain = hub.api.node("kitchen-relay")
        assert not hub.inst.installation.nodes[RELAY].described
        assert hub.inst.core.enumeration.started == 0, "a plain view reads nothing"

        described = hub.api.node("kitchen-relay", describe=True)

        assert 4 not in {ep.number for ep in plain.detail if ep.type_id is not None}
        assert any(ep.number == 4 and ep.type_name == "Output" for ep in described.detail)
        assert hub.inst.installation.nodes[RELAY].described

    def test_asked_to_read_a_silent_one_says_so(
        self, clock: Clock, tmp_path: Path, joined: tuple[Hub, int]
    ) -> None:
        """Silence, said as silence — not "no endpoint that reports anything"."""
        hub = _restart(clock, joined[0], tmp_path / "after")
        relay = _back(hub, clock, joined[1])
        relay.faults.silent_until = clock.now + 60.0

        with pytest.raises(ApiError) as error:
            hub.api.node("kitchen-relay", describe=True)

        assert error.value.problem is Problem.SILENT

    def test_its_first_frame_reads_what_it_is(
        self, clock: Clock, tmp_path: Path, joined: tuple[Hub, int]
    ) -> None:
        """The frame that comes anyway is the occasion — and reconciling follows, so a
        change made to the file while the hub was down reaches the node."""
        hub = _restart(clock, joined[0], tmp_path / "after")
        relay = _back(hub, clock, joined[1])

        relay.send_report(1, 0x00)
        hub.inst.run(1.0)

        node = hub.inst.installation.nodes[RELAY]
        assert node.described
        assert node.endpoints[4].type_id is not None
        assert hub.inst.core.enumeration.completed == 1
        assert hub.inst.core.reconcile.checked > 0, "and reconciling followed"

    def test_no_frame_no_traffic(
        self, clock: Clock, tmp_path: Path, joined: tuple[Hub, int]
    ) -> None:
        """Not polling everybody at start (07-hub.md §5.1)."""
        hub = _restart(clock, joined[0], tmp_path / "after")
        _back(hub, clock, joined[1])

        hub.inst.run(2.0)

        assert hub.inst.core.enumeration.started == 0
        assert sum(hub.inst.core.counts.frames) == 0

    def test_a_silent_one_is_an_answer_not_a_hang(
        self, clock: Clock, tmp_path: Path, joined: tuple[Hub, int]
    ) -> None:
        hub = _restart(clock, joined[0], tmp_path / "after")
        relay = _back(hub, clock, joined[1])
        relay.faults.silent_until = clock.now + 60.0

        with pytest.raises(ApiError) as error:
            hub.api.command("kitchen-relay", 4, "Toggle")

        assert error.value.problem is Problem.SILENT
        assert hub.inst.installation.nodes[RELAY].state == NodeState.READY, (
            "silence is not removal — the node stays where the file puts it"
        )

    def test_back_from_a_factory_reset_it_gets_its_address_without_the_mode(
        self, clock: Clock, tmp_path: Path, joined: tuple[Hub, int]
    ) -> None:
        """It announces nothing but `ShortId` — which is why the file keeps it
        (06-cykl-zycia-urzadzenia.md §7)."""
        hub = _restart(clock, joined[0], tmp_path / "after")
        relay = hub.inst.add(relay1(uid=RELAY_UID, address=0, clock=clock, name="relay"))
        assert not hub.inst.core.commissioning.accepting

        hub.inst.run(5.0, step=0.05)

        assert relay.address == joined[1]
        assert hub.inst.installation.nodes[RELAY].described


class TestKnownUid:
    """D4: the file is older than the bus — the node has an address the hub never wrote
    down."""

    def test_a_node_from_the_file_is_let_in_without_asking(
        self, clock: Clock, tmp_path: Path
    ) -> None:
        hub = Hub(clock, tmp_path)
        relay = _back(hub, clock, 0x040)

        relay.send_report(1, 0x00)
        hub.inst.run(1.0)

        node = hub.inst.installation.nodes[RELAY]
        assert node.state == NodeState.READY
        assert node.address == 0x040
        assert hub.inst.core.strangers.known == 1
        assert hub.inst.core.strangers.pending == 0
        entry = load(hub.path).node(RELAY)
        assert entry is not None and entry.joined is not None
        assert entry.joined.address == 0x040, "and from now on the file knows"

    def test_a_blocked_one_stays_blocked(self, clock: Clock, tmp_path: Path) -> None:
        content = CONFIG.replace(
            'alias: kitchen-relay\n    name: "The lamp over the table"',
            'alias: kitchen-relay\n    blocked: true\n    name: "The lamp over the table"',
        )
        hub = Hub(clock, tmp_path, content)
        relay = _back(hub, clock, 0x040)

        relay.send_report(1, 0x00)
        hub.inst.run(1.0)

        assert hub.inst.installation.records[short_id(RELAY_UID)].state == NodeState.BLOCKED
        assert RELAY not in hub.inst.installation.nodes


class TestLoading:
    def test_joined_in_hex_as_text_or_as_a_number(self) -> None:
        config = from_document(
            {
                "nodes": {
                    "0102030405060708": {
                        "joined": {"address": 0x12, "bus": "b", "short_id": "0x5A1B2C3D"}
                    }
                }
            }
        )
        joined = config.nodes[0].joined
        assert joined is not None
        assert (joined.address, joined.bus, joined.short_id) == (0x12, "b", 0x5A1B2C3D)

    @pytest.mark.parametrize(
        ("joined", "says"),
        [
            ({"address": 0x7FE, "bus": "b", "short_id": 1}, "not a node address"),
            ({"address": 0, "bus": "b", "short_id": 1}, "not a node address"),
            ({"address": 0x12, "short_id": 1}, "lacks bus"),
            ({"address": "kitchen", "bus": "b", "short_id": 1}, "is a number"),
            ({"address": 0x12, "bus": "b", "short_id": 1 << 32}, "32 bits"),
            ("0x012", "is a mapping"),
        ],
    )
    def test_a_broken_one_stops_the_file(self, joined: object, says: str) -> None:
        """What the hub wrote is checked like anything a person wrote — a hand edit that
        breaks it must not put a node at an address it does not hold."""
        with pytest.raises(ConfigError, match=says):
            from_document({"nodes": {"0102030405060708": {"joined": joined}}})

    def test_the_file_and_the_bus_disagreeing_is_an_issue(self) -> None:
        """Two entries claiming one address — a copied entry, say. The hub does not pick
        one."""
        config = from_document(
            {
                "nodes": {
                    "0102030405060708": {"joined": {"address": 0x12, "bus": "b", "short_id": 1}},
                    "1112131415161718": {"joined": {"address": 0x12, "bus": "b", "short_id": 2}},
                }
            }
        )
        installation = Installation()
        apply(installation, config, ["b"])

        assert len(installation.nodes) == 1
        assert installation.issues and "0x012" in installation.issues[0]

    def test_a_reload_does_not_repeat_the_issue(self) -> None:
        config = from_document(
            {
                "buses": [{"name": "a", "channel": "can0"}, {"name": "b", "channel": "can1"}],
                "nodes": {
                    "0102030405060708": {"joined": {"address": 0x12, "bus": "x", "short_id": 1}}
                },
            }
        )
        installation = Installation()
        apply(installation, config, ["can0", "can1"])
        apply(installation, config, ["can0", "can1"])

        assert len(installation.issues) == 1
        assert not installation.nodes, "found at its first frame instead"


class TestBuses:
    """N8 — the bus by the name in `hub.yaml`."""

    CONFIG = from_document(
        {
            "buses": [
                {"name": "ground-floor", "channel": "can0"},
                {"name": "attic", "channel": "can1"},
            ]
        }
    )

    def test_by_the_name_in_the_file(self) -> None:
        assert bus_index(self.CONFIG, ["can0", "can1"], "attic") == 1

    def test_by_the_open_bus_name(self) -> None:
        assert bus_index(self.CONFIG, ["can0", "can1"], "can1") == 1

    def test_one_bus_takes_whatever(self) -> None:
        """Renamed in `hub.yaml`, or a trial on `--udp` with a real configuration."""
        assert bus_index(self.CONFIG, ["udp"], "ground-floor") == 0

    def test_several_and_no_match_is_no_guess(self) -> None:
        assert bus_index(self.CONFIG, ["can0", "can1"], "cellar") is None


def test_set_joined_keeps_comments(tmp_path: Path) -> None:
    """The file is a person's — the hub's line goes in without taking anything out."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "nodes:\n  '0102030405060708':\n    alias: lamp  # over the table\n", encoding="utf-8"
    )
    store = ConfigStore(path)

    assert store.set_joined(bytes(range(1, 9)), 0x12, "can0", 0xAB) is not None
    assert store.set_joined(bytes(range(1, 9)), 0x12, "can0", 0xAB) is None

    text = path.read_text(encoding="utf-8")
    assert "# over the table" in text
    assert "joined: {address: 0x012, bus: can0, short_id: 0x000000AB}" in text
