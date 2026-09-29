"""The shell and its content.

`commands.py` is tested **without a process, without a port and without a terminal**:
`Hub` is a protocol, so in a test anything with those methods will do. A separate class
at the end checks the same thing over real HTTP — because the client is the one place
where the shape of the hub's replies has to agree field by field.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from lagunhub.adapters.cli import commands, render
from lagunhub.adapters.cli.client import HubClient
from lagunhub.adapters.cli.shell import Shell
from lagunhub.api import (
    ActionView,
    ApiError,
    ArgView,
    AttributeView,
    BindingView,
    CommandView,
    CommissioningView,
    ConfigView,
    DiffRow,
    EndpointView,
    EventView,
    HealthView,
    HubApi,
    HubInfo,
    NodeSummary,
    NodeView,
    Problem,
    ReconcileView,
    TargetView,
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
        AttributeView(id=0x00, name="OnOff", type="bool", access="RO", value=False),
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
    ),
    events=("Down", "Up", "Click", "HoldStart"),
)


class FakeHub:
    """As much hub as the shell sees. Nothing here runs and nothing listens on a port."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
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


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub()


def test_hubapi_and_hubclient_satisfy_the_same_protocol() -> None:
    """If they stopped, the shell would work with only one side — and that would come out
    only at a person's terminal."""

    def takes_it(_: commands.Hub) -> None: ...

    takes_it(FakeHub())
    # A static check: mypy will reject this file if either of them drifts.
    _: type[commands.Hub] = HubApi
    __: type[commands.Hub] = HubClient


class TestCommands:
    def test_admit_and_reset_take_the_short_id_as_joining_prints_it(self, hub: FakeHub) -> None:
        """`joining` prints `0xAABBCCDD`; a person copies that — and it is to work, with or
        without the prefix."""
        commands.run(hub, "admit 0xAABBCCDD")
        commands.run(hub, "reset aabbccdd")
        assert ("admit", (0xAABBCCDD,)) in hub.calls
        assert ("factory_reset", (0xAABBCCDD,)) in hub.calls

    def test_a_short_id_that_is_not_hex_is_refused(self, hub: FakeHub) -> None:
        with pytest.raises(ApiError) as caught:
            commands.run(hub, "admit kitchen")
        assert caught.value.problem == Problem.INVALID

    def test_cmd_goes_straight_to_the_node(self, hub: FakeHub) -> None:
        result = commands.run(hub, "cmd kitchen-relay ep4 Toggle")
        assert result.kind == "action"
        assert ("command", ("kitchen-relay", 4, "Toggle", {})) in hub.calls

    def test_an_endpoint_number_may_be_written_two_ways(self, hub: FakeHub) -> None:
        """The hub prints `ep4`, so a person will copy that — and it is to work."""
        commands.run(hub, "cmd kitchen-relay 4 Toggle")
        commands.run(hub, "cmd kitchen-relay ep4 Toggle")
        assert [w for w in hub.calls if w[0] == "command"] == [
            ("command", ("kitchen-relay", 4, "Toggle", {})),
        ] * 2

    def test_command_arguments(self, hub: FakeHub) -> None:
        commands.run(hub, "cmd kitchen-relay ep4 SetLevel level=128 fade=10")
        assert ("command", ("kitchen-relay", 4, "SetLevel", {"level": 128, "fade": 10})) in (
            hub.calls
        )

    def test_set_goes_to_the_file_and_write_to_the_node(self, hub: FakeHub) -> None:
        """A distinction that can hurt in an installation (KONFIGURACJA.md §11.1)."""
        commands.run(hub, "set kitchen-relay ep4 AutoOffTime 30")
        commands.run(hub, "write kitchen-relay ep4 Level 128")

        assert (
            "config_set_attribute",
            ("kitchen-relay", 4, "AutoOffTime", 30, "sha256:aaa"),
        ) in hub.calls
        assert ("write", ("kitchen-relay", 4, "Level", 128)) in hub.calls

    def test_the_write_path_uses_the_uid_not_the_alias(self, hub: FakeHub) -> None:
        """An alias is a label and may be changed; the path in the file is assembled by
        **the hub**, from the `Uid` — the CLI gives it only what the person pointed at."""
        commands.run(hub, "set kitchen-relay ep4 AutoOffTime 30")
        assert any(w[0] == "config_set_attribute" for w in hub.calls)

    def test_a_config_write_goes_through_the_operation_that_adds_the_type(
        self, hub: FakeHub
    ) -> None:
        """On a fresh configuration an attribute alone will not save — the loader requires
        a `type`."""
        commands.run(hub, "set kitchen-relay ep4 AutoOffTime 30")
        assert not any(w[0] == "config_set" for w in hub.calls)

    def test_values_keep_their_type(self, hub: FakeHub) -> None:
        """`30` is to land in the YAML as a number, not as `"30"`."""
        commands.run(hub, "set kitchen-relay ep4 AutoOffTime 30")
        commands.run(hub, "write kitchen-relay ep4 Level true")
        commands.run(hub, "write kitchen-relay ep4 Purpose LIGHT")
        values = [w[1][-1] for w in hub.calls if w[0] == "write"]
        assert values == [True, "LIGHT"]
        assert next(w[1][3] for w in hub.calls if w[0] == "config_set_attribute") == 30

    def test_a_duration_may_be_given_in_human_terms(self, hub: FakeHub) -> None:
        commands.run(hub, "accept 15m")
        commands.run(hub, "accept 1h")
        assert [w[1][0] for w in hub.calls if w[0] == "accept_new"] == [900.0, 3600.0]

    def test_a_missing_argument_says_what_it_is_about(self, hub: FakeHub) -> None:
        with pytest.raises(ApiError) as error:
            commands.run(hub, "cmd kitchen-relay")
        assert "command" in error.value.message
        assert "Usage:" in error.value.message

    def test_an_unknown_command_lists_the_known_ones(self, hub: FakeHub) -> None:
        with pytest.raises(ApiError) as error:
            commands.run(hub, "light")
        assert "nodes" in error.value.message

    def test_a_hub_error_passes_through_unchanged(self, hub: FakeHub) -> None:
        with pytest.raises(ApiError) as error:
            commands.run(hub, "show cellar")
        assert error.value.problem is Problem.NOT_FOUND


class TestCompletion:
    """The test of whether the hub API exposes enough — whatever cannot be completed is not
    there."""

    def test_an_empty_line_completes_commands(self, hub: FakeHub) -> None:
        assert "nodes" in _candidates(hub, "no", "no")

    def test_nodes_come_from_the_installation(self, hub: FakeHub) -> None:
        assert _candidates(hub, "show ", "") == ["hall-switch", "kitchen-relay"]

    def test_endpoints_come_from_the_node(self, hub: FakeHub) -> None:
        assert _candidates(hub, "ep kitchen-relay ", "") == ["ep0", "ep4"]

    def test_commands_come_from_the_type_not_from_the_cli(self, hub: FakeHub) -> None:
        """The shell knows nothing about the `Output` type — it gets the list from the
        hub."""
        assert _candidates(hub, "cmd kitchen-relay ep4 Tog", "Tog") == ["Toggle"]

    def test_values_come_from_choices(self, hub: FakeHub) -> None:
        assert _candidates(hub, "set kitchen-relay ep4 Purpose ", "") == [
            "GENERIC",
            "INDICATOR",
            "LIGHT",
            "VALVE",
        ]

    def test_set_suggests_configuration_only(self, hub: FakeHub) -> None:
        """`OnOff` is state, `Level` a working setting — under `set` they would be a
        straight road into the error "that is state, not configuration"."""
        assert _candidates(hub, "set kitchen-relay ep4 ", "") == ["AutoOffTime", "Purpose"]

    def test_write_suggests_what_is_writable(self, hub: FakeHub) -> None:
        assert _candidates(hub, "write kitchen-relay ep4 ", "") == [
            "AutoOffTime",
            "Level",
            "Purpose",
        ]

    def test_after_the_last_argument_nothing_is_suggested(self, hub: FakeHub) -> None:
        assert _candidates(hub, "identify kitchen-relay foo ", "") == []

    def test_an_unknown_node_does_not_break_the_tab_key(self, hub: FakeHub) -> None:
        assert _candidates(hub, "ep cellar ", "") == []


class TestRendering:
    def test_unknown_does_not_look_like_zero(self, hub: FakeHub) -> None:
        """The hub does not poll nodes after they join, so "I do not know" is an honest
        answer — and it is to differ from `no`."""
        text = render.render(commands.run(hub, "ep kitchen-relay ep4"))
        row = next(w for w in text.split("\n") if "AutoOffTime" in w)
        assert render.MISSING in row
        onoff = next(w for w in text.split("\n") if "OnOff" in w)
        assert "no" in onoff and render.MISSING in onoff, (
            "the value is known, the configuration is not"
        )

    def test_the_drift_is_visible(self, hub: FakeHub) -> None:
        text = render.render(commands.run(hub, "diff"))
        assert "DRIFT" in text
        assert "LIGHT" in text and "VALVE" in text
        assert "1 disagree with the file (of 1)" in text

    def test_unread_is_not_the_same_as_drift(self, hub: FakeHub) -> None:
        """The first means "the hub has not read it yet", the second "the node has
        something else". Gluing them into one number would send one looking for a fault
        where there is none."""
        hub.drift = (
            DiffRow(
                uid=RELAY,
                alias="kitchen-relay",
                endpoint=4,
                attribute="AutoOffTime",
                state="unknown",
                wanted=30,
            ),
        )
        text = render.render(commands.run(hub, "diff"))
        assert "disagree" not in text
        assert "1 not read yet" in text

    def test_a_binding_shows_source_and_target_not_the_target_twice(self, hub: FakeHub) -> None:
        """One binding has two ends and both have to be nameable.

        `BindingSpec` had one `label` field — the target's — so the bindings view drew an
        arrow from the target to the target. That showed up only in the shell.
        """
        hub.binding_rows = (
            BindingView(
                source_uid="00" * 8,
                source_alias="hall-switch",
                source_endpoint=4,
                label="hall-switch ep4 Click",
                targets=(
                    TargetView(
                        kind="COMMAND",
                        endpoint=4,
                        element=2,
                        alias="kitchen-relay",
                        label="kitchen-relay ep4 Output.Toggle",
                        active=True,
                    ),
                ),
            ),
        )
        text = render.render(commands.run(hub, "bindings"))
        assert "hall-switch ep4 Click  →  kitchen-relay ep4 Output.Toggle" in text

    def test_the_table_has_even_columns(self, hub: FakeHub) -> None:
        lines = render.render(commands.run(hub, "nodes")).split("\n")
        assert lines[0].index("UID") == lines[1].index(RELAY)

    def test_an_event_fits_in_one_row(self) -> None:
        line = render.event(
            EventView(kind="state", at=0.0, uid=RELAY, endpoint=4, name="OnOff", value=True)
        )
        assert len(line) < 60
        assert "OnOff" in line and "yes" in line


class TestShell:
    def test_it_runs_a_command_and_prints(self, hub: FakeHub) -> None:
        output = io.StringIO()
        shell = Shell(hub, out=output)
        assert shell.execute("nodes") is True
        assert "kitchen-relay" in output.getvalue()

    def test_an_error_does_not_end_the_session(self, hub: FakeHub) -> None:
        output = io.StringIO()
        shell = Shell(hub, out=output)
        assert shell.execute("show cellar") is True
        assert "! there is no node" in output.getvalue()

    def test_quit_ends_it(self, hub: FakeHub) -> None:
        assert Shell(hub, out=io.StringIO()).execute("quit") is False

    def test_help_lists_the_commands_and_tells_set_from_write(self, hub: FakeHub) -> None:
        output = io.StringIO()
        Shell(hub, out=output).execute("help")
        text = output.getvalue()
        assert "identify" in text
        assert "set  goes to the FILE" in text
        assert "write goes STRAIGHT to the node" in text

    def test_the_feed_to_a_pipe_sends_no_control_sequences(self, hub: FakeHub) -> None:
        """Printing from under the prompt clears the line with an ANSI sequence. In a file
        or a pipe it would stay there as `[2K` — that is, rubbish in the log."""
        output = io.StringIO()  # a StringIO is not a terminal
        shell = Shell(hub, out=output)
        shell._emit("  something happened")
        assert "\x1b" not in output.getvalue()
        assert "something happened" in output.getvalue()

    def test_an_empty_line_does_nothing(self, hub: FakeHub) -> None:
        output = io.StringIO()
        Shell(hub, out=output).execute("   ")
        assert output.getvalue() == ""


def _candidates(hub: FakeHub, line: str, prefix: str) -> list[str]:
    return Shell(hub, out=io.StringIO()).candidates(line, prefix)
