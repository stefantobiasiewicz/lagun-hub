"""The full-screen view — state, the panes’ content, and the layout.

Three layers, three different ways of checking them:

| | |
|---|---|
| `state.py` | the feed’s backpressure and navigation — plain assertions |
| `panes.py` | **row content without a single `curses`** — columns, colours, dashes |
| `screen.py` | the layout, through a fake screen that remembers what was drawn where |

The layout and why it is so are in [`ARCHITEKTURA.md`](../ARCHITEKTURA.md) §10.3.3.
"""

from __future__ import annotations

import curses
from dataclasses import replace
from typing import Any

import pytest

from lagunhub.adapters.tui import app, overlay, panes, screen
from lagunhub.adapters.tui.state import Feed, Focus, Section, State
from lagunhub.api import (
    AttributeView,
    BindingView,
    BusSetupView,
    BusView,
    ChangeView,
    CommissioningView,
    HealthView,
    JoinView,
    NodeSummary,
    TargetView,
    TrafficView,
)
from tests.fake_hub import EP4, RELAY, FakeHub


@pytest.fixture
def state() -> State:
    state = State(FakeHub())
    state.reload()
    return state


@pytest.fixture
def expanded(state: State) -> State:
    """The cursor on `kitchen-relay`, the details panel showing its endpoints."""
    state.worker._on_done = lambda _: None
    state.snapshot = replace(state.snapshot, node=state.hub.node("kitchen-relay"))
    return state


class FakeScreen:
    """A screen that remembers what was drawn on it.

    `curses` is faked here for one reason: **the layout arithmetic is what is easiest to
    get wrong**, and the only way to check it is to see which row what landed in. A real
    terminal would answer the same question only after a run, and only for one window
    size.
    """

    def __init__(self, height: int = 30, width: int = 110) -> None:
        self.height = height
        self.width = width
        self.erase()

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.grid = [" " * self.width for _ in range(self.height)]

    def refresh(self) -> None: ...

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        del attr
        if not 0 <= y < self.height:
            raise AssertionError(f"drawing off the screen: row {y} of {self.height}")
        if x + len(text) > self.width:
            raise AssertionError(f"past the width: {x}+{len(text)} > {self.width}")
        row = self.grid[y]
        self.grid[y] = (row[:x] + text + row[x + len(text) :])[: self.width]

    def find(self, fragment: str) -> int:
        for i, row in enumerate(self.grid):
            if fragment in row:
                return i
        raise AssertionError(f"not on the screen: {fragment!r}\n" + "\n".join(self.grid))

    def absent(self, fragment: str) -> None:
        assert not any(fragment in r for r in self.grid), f"there, and should not be: {fragment!r}"


class TestBackpressure:
    """Backpressure belongs to the view: it drops at its own end and **says how many**."""

    def test_the_buffer_drops_the_oldest_and_counts(self) -> None:
        feed = Feed(depth=10)
        for i in range(25):
            feed.add(TrafficView(at=float(i), bus="hub", kind="REPORT", outgoing=False, tid=i))
        assert len(feed.items) == 10
        assert feed.items[0].tid == 15, "the newest ones stayed"
        assert feed.dropped == 15

    def test_a_paused_feed_does_not_grow(self) -> None:
        """A pause is to stop the picture, not the memory — otherwise forgetting that
        space was pressed is enough for the view to eat the RAM."""
        feed = Feed(depth=10)
        feed.paused = True
        for i in range(50):
            feed.add(TrafficView(at=float(i), bus="hub", kind="REPORT", outgoing=False))
        assert feed.items == []
        assert feed.rate > 0, "the rate is still counted — one sees something is flowing"


class TestNavigation:
    """The list **never disappears** — that was the first version’s main flaw."""

    def test_entering_moves_the_cursor_to_details_and_the_list_stays(self, expanded: State) -> None:
        before = expanded.list_rows()
        expanded.enter()
        assert expanded.focus is Focus.DETAIL
        assert expanded.list_rows() == before, "the node list stays where it was"
        assert [r.number for r in expanded.detail_rows()] == [0, 4]

    def test_entering_again_shows_the_attributes(self, expanded: State) -> None:
        _into_ep4(expanded)
        assert expanded.endpoint == 4
        assert all(isinstance(r, AttributeView) for r in expanded.detail_rows())

    def test_back_retreats_step_by_step_and_ends(self, expanded: State) -> None:
        """The last "back" is the way out — otherwise one cannot leave with the key one
        was retreating with."""
        _into_ep4(expanded)
        assert expanded.back(), "it collapses the attributes first"
        assert expanded.back(), "then the cursor returns to the list"
        assert expanded.back(), "then to the sections"
        assert not expanded.back(), "and above that there is nothing — that is the way out"

    def test_arrows_in_the_details_do_not_move_the_list(self, expanded: State) -> None:
        _into_ep4(expanded)  # into the attributes, because there is something to walk over
        before = expanded.cursor[Section.NODES]
        expanded.move(1)
        assert expanded.cursor[Section.NODES] == before, "the list stays put"
        assert expanded.detail_cursor == 1

    def test_changing_section_collapses_the_details(self, expanded: State) -> None:
        expanded.enter()
        expanded.section_by(1)
        assert expanded.section is Section.BINDINGS
        assert expanded.focus is Focus.LIST and expanded.endpoint is None


class TestLists:
    def test_a_node_without_an_address_is_dimmed(self, state: State) -> None:
        pane = panes.items(state)
        assert pane.rows[0].colour == panes.GOOD
        assert pane.rows[1].colour == panes.DIM

    def test_a_missing_address_is_a_dash_not_a_zero(self, state: State) -> None:
        assert panes.items(state).rows[1].cells[2] == panes.MISSING

    def test_drift_is_red(self, state: State) -> None:
        state.section = Section.DIFF
        row = panes.items(state).rows[0]
        assert row.colour == panes.BAD
        assert row.cells[-1] == "DRIFT"
        assert "LIGHT" in row.cells and "VALVE" in row.cells

    def test_a_binding_without_a_target_is_red(self, state: State) -> None:
        state.section = Section.BINDINGS
        state.snapshot = replace(
            state.snapshot,
            bindings=(
                BindingView(
                    source_uid="00" * 8,
                    label="hall-switch ep4 Click",
                    targets=(
                        TargetView(
                            kind="COMMAND", endpoint=4, element=2, label="target", active=False
                        ),
                    ),
                ),
            ),
        )
        row = panes.items(state).rows[0]
        assert row.colour == panes.BAD
        assert row.cells == ("hall-switch ep4 Click", "target", "TARGET HAS NO ADDRESS")

    def test_a_filter_narrows_the_list(self, state: State) -> None:
        state.filter = "relay"
        assert [r.cells[0] for r in panes.items(state).rows] == ["kitchen-relay"]

    def test_a_filter_with_no_hits_says_how_to_clear_it(self, state: State) -> None:
        state.filter = "dishwasher"
        assert "Esc clears it" in panes.items(state).empty


class TestDetails:
    def test_endpoints_summarise_the_most_important_value(self, expanded: State) -> None:
        pane = panes.details(expanded)
        assert pane.title.startswith("kitchen-relay")
        assert [r.cells[0] for r in pane.rows] == ["ep0", "ep4"]
        assert "OnOff = no" in pane.rows[1].cells[-1]

    def test_attributes_show_the_node_next_to_the_file(self, expanded: State) -> None:
        _into_ep4(expanded)
        pane = panes.details(expanded)
        purpose = next(r for r in pane.rows if r.cells[0] == "Purpose")
        assert purpose.cells[1:4] == ("LIGHT", "LIGHT", "✓")
        assert purpose.cells[5] == "configuration"

    def test_unknown_does_not_look_like_zero(self, expanded: State) -> None:
        _into_ep4(expanded)
        pane = panes.details(expanded)
        auto = next(r for r in pane.rows if r.cells[0] == "AutoOffTime")
        assert auto.cells[1] == panes.MISSING, "the hub does not know the value"
        assert auto.cells[3] == "", "there is nothing to compare with"

    def test_drift_explains_the_direction_of_reconciling(self, state: State) -> None:
        state.section = Section.DIFF
        pane = panes.details(state)
        assert any("never the other way round" in line for line in pane.note)
        assert ("R", "reconcile now") in panes.context_keys(state)


class TestHealthDetail:
    """Traffic per bus, one line each (zadania/zrobione/FEAT-05.md)."""

    def health(self, state: State, *buses: BusView) -> list[str]:
        view = HealthView(nodes=1, ready=1, configured=1, issues=0, buses=buses)
        state.snapshot = replace(state.snapshot, health=view)
        state.section = Section.HEALTH
        return list(panes.details(state).note)

    def test_a_line_per_bus_with_rates_load_and_counters(self, state: State) -> None:
        lines = self.health(
            state,
            BusView(
                0,
                "can0",
                rx=10234,
                tx=2210,
                dropped=1,
                rx_rate=12.3,
                tx_rate=3.0,
                bitrate=125_000,
                load=0.0123,
            ),
            BusView(1, "can1"),
        )
        can0 = next(line for line in lines if line.startswith("can0"))
        assert "rx 12.3/s" in can0 and "tx 3.0/s" in can0
        assert "≈1.2 %" in can0, "an estimate, and it says so"
        assert "rx 10234 · tx 2210 · dropped 1" in can0
        assert any(line.startswith("can1") for line in lines)

    def test_without_a_bitrate_there_is_no_percentage(self, state: State) -> None:
        lines = self.health(state, BusView(0, "udp", rx_rate=4.0))
        udp = next(line for line in lines if line.startswith("udp"))
        assert "%" not in udp
        assert panes.MISSING in udp

    def test_a_bus_the_hub_gave_up_on_says_so(self, state: State) -> None:
        lines = self.health(state, BusView(0, "can0", link="gave-up"), BusView(1, "can1"))
        assert next(line for line in lines if line.startswith("can0")).endswith("GAVE UP")
        assert not next(line for line in lines if line.startswith("can1")).endswith("UP")

    def test_no_bus_says_so(self, state: State) -> None:
        lines = self.health(state)
        assert f"buses: {panes.MISSING}" in lines


class TestContextKeys:
    """The action keys change with what is selected — there is nothing to remember."""

    def test_on_an_output_there_is_a_toggle(self, expanded: State) -> None:
        context_keys = dict(panes.context_keys(expanded))
        assert context_keys["t"] == "Toggle"
        assert context_keys["i"] == "Identify"

    def test_on_a_config_attribute_there_is_an_edit(self, expanded: State) -> None:
        _into_ep4(expanded)
        expanded.detail_cursor = _index(expanded, "Purpose")
        context_keys = dict(panes.context_keys(expanded))
        assert context_keys["e"] == "edit in the file"
        assert context_keys["w"] == "set on the node"

    def test_on_state_there_is_no_edit_in_the_file(self, expanded: State) -> None:
        """`OnOff` is state — hinting at `e` would lead straight into an error."""
        _into_ep4(expanded)
        expanded.detail_cursor = _index(expanded, "OnOff")
        context_keys = dict(panes.context_keys(expanded))
        assert "e" not in context_keys
        assert "w" not in context_keys, "OnOff is RO"

    def test_adopt_appears_only_on_drift(self, expanded: State) -> None:
        _into_ep4(expanded)
        expanded.detail_cursor = _index(expanded, "Purpose")
        assert "p" not in dict(panes.context_keys(expanded)), "it agrees, nothing to adopt"


class TestTraffic:
    def test_a_row_carries_direction_kind_and_time(self) -> None:
        from tests.fake_hub import TRAFFIC

        command, response = (
            panes.traffic_row(TRAFFIC[0])[0],
            panes.traffic_row(TRAFFIC[1])[0],
        )
        assert "→" in command and "COMMAND" in command and "tid=1" in command
        assert "0x001 ep4" in command and "Toggle" in command
        assert "RESPONSE" in response and "OK" in response and "3.1 ms" in response

    def test_a_late_reply_says_so(self) -> None:
        """An answer after its transaction closed — the hub retried (BUG-03)."""
        from dataclasses import replace

        from tests.fake_hub import TRAFFIC

        late = panes.traffic_row(replace(TRAFFIC[1], elapsed=0.1081, late=True))[0]
        assert late.endswith("108.1 ms late") and "Toggle OK" in late
        assert "late" not in panes.traffic_row(TRAFFIC[1])[0]

    def test_a_retry_says_so(self) -> None:
        """On the wire a retry is the same frame with the same `Tid` (BUG-03)."""
        from dataclasses import replace

        from tests.fake_hub import TRAFFIC

        retry = panes.traffic_row(replace(TRAFFIC[0], elapsed=0.1002, retry=1))[0]
        assert "→" in retry and "Toggle" in retry and retry.endswith("100.2 ms retry 1")
        assert "retry" not in panes.traffic_row(TRAFFIC[0])[0]

    def test_a_refusal_is_red(self) -> None:
        from tests.fake_hub import TRAFFIC

        assert panes.traffic_row(TRAFFIC[1])[1] == panes.NORMAL
        assert panes.traffic_row(TRAFFIC[2])[1] == panes.BAD, "BUSY is a refusal"


class TestLayout:
    def test_the_frame_is_closed(self, state: State) -> None:
        screen_ = FakeScreen()
        screen.draw(screen_, state)
        assert screen_.grid[0].startswith("┌") and screen_.grid[0].rstrip().endswith("┐")
        assert screen_.grid[28].startswith("└")

    def test_all_sections_are_visible(self, state: State) -> None:
        screen_ = FakeScreen()
        screen.draw(screen_, state)
        for section in Section:
            screen_.find(section.value)

    def test_the_list_and_the_details_show_at_once(self, expanded: State) -> None:
        """The heart of the fix: entering a node **does not hide the installation**."""
        expanded.enter()
        screen_ = FakeScreen()
        screen.draw(screen_, expanded)
        assert screen_.find("kitchen-relay") < screen_.find("ep4")
        screen_.find("hall-switch")

    def test_the_feed_and_the_hints_are_in_their_places(self, state: State) -> None:
        screen_ = FakeScreen(height=30)
        screen.draw(screen_, state)
        assert screen_.find("Traffic on the bus") == 19, "the feed takes the bottom third"
        assert screen_.find("q quit") == 29, "the hints are always in the last row"

    def test_the_counter_does_not_blend_into_the_section_name(self, state: State) -> None:
        state.snapshot = replace(state.snapshot, issues=tuple(f"u{i}" for i in range(12)))
        screen_ = FakeScreen()
        screen.draw(screen_, state)
        row = screen_.grid[screen_.find("Issues")][: screen.NAV_W + 1]
        assert "Issues" in row and "12" in row
        gap = row.index("12") - (row.index("Issues") + len("Issues"))
        assert gap >= 1, f"the digits blend into the name: {row!r}"

    def test_a_small_window_says_so_instead_of_falling_over(self, state: State) -> None:
        screen.draw(FakeScreen(height=8, width=40), state)
        screen.draw(FakeScreen(height=40, width=20), state)

    def test_every_window_size_can_be_drawn(self, expanded: State) -> None:
        _into_ep4(expanded)
        for height in range(12, 60, 5):
            for width in (50, 64, 80, 110, 200):
                screen.draw(FakeScreen(height=height, width=width), expanded)

    def test_long_lists_scroll_to_the_cursor(self, state: State) -> None:
        state.section = Section.ISSUES
        state.snapshot = replace(state.snapshot, issues=tuple(f"issue {i}" for i in range(50)))
        state.cursor[Section.ISSUES] = 40
        screen_ = FakeScreen(height=24)
        screen.draw(screen_, state)
        screen_.find("issue 40")


class TestOverlays:
    def test_a_named_value_is_chosen_from_a_list(self, expanded: State) -> None:
        """Names are not guessed — `choices` comes from the hub."""
        _into_ep4(expanded)
        expanded.detail_cursor = _index(expanded, "Purpose")
        app._edit(expanded)
        assert isinstance(expanded.overlay, overlay.Choose)
        assert expanded.overlay.options == EP4.attributes[1].choices

    def test_a_numeric_value_is_typed_in(self, expanded: State) -> None:
        _into_ep4(expanded)
        expanded.detail_cursor = _index(expanded, "AutoOffTime")
        app._edit(expanded)
        assert isinstance(expanded.overlay, overlay.Edit)

    def test_editing_state_is_refused_with_a_reason(self, expanded: State) -> None:
        _into_ep4(expanded)
        expanded.detail_cursor = _index(expanded, "OnOff")
        app._edit(expanded)
        assert expanded.overlay is None
        assert "is state" in expanded.status

    def test_confirmation_requires_retyping_the_name(self) -> None:
        performed: list[Any] = []
        window = overlay.Confirm(
            title="Are you sure?",
            lines_before=("it will erase everything",),
            expect="kitchen-relay",
            on_accept=performed.append,
        )
        for sign in "kitchen":
            window.key(ord(sign))
        assert window.key(10) is True, "too early — the window stays"
        assert not performed
        for sign in "-relay":
            window.key(ord(sign))
        assert window.key(10) is False
        assert performed == ["kitchen-relay"]

    def test_esc_closes_without_saving(self) -> None:
        saved: list[Any] = []
        window = overlay.Edit(title="t", subject="s", hint="h", on_accept=saved.append)
        window.key(ord("4"))
        assert window.key(27) is False
        assert not saved

    def test_an_overlay_captures_the_keys(self, state: State) -> None:
        """While a window is on top, `q` types a letter rather than quitting."""
        state.overlay = overlay.Edit(title="t", subject="s", hint="h", on_accept=lambda _: None)
        assert app.key(state, ord("q")) is True
        assert state.overlay is not None


class TestCursorMatchesWhatIsShown:
    """The worst view fault imaginable: `e` edits **the wrong** item.

    The pane filters rows, so if the state counted positions its own way, the highlight
    would point at something other than the selection. One source of truth: `Pane.items`.
    """

    def test_the_selected_attributes_are_the_ones_drawn(self, expanded: State) -> None:
        _into_ep4(expanded)
        pane = panes.details(expanded)
        for i in range(len(pane.rows)):
            expanded.detail_cursor = i
            assert expanded.detail_selected().name == pane.rows[i].cells[0]

    def test_the_filter_does_not_desync_the_list_from_the_selection(self, state: State) -> None:
        state.filter = "hall"
        pane = panes.items(state)
        assert len(pane.rows) == 1
        state.cursor[Section.NODES] = 0
        assert state.selected().alias == pane.rows[0].cells[0] == "hall-switch"

    def test_the_edit_applies_to_the_highlighted_one(self, expanded: State) -> None:
        _into_ep4(expanded)
        pane = panes.details(expanded)
        expanded.detail_cursor = next(
            i for i, r in enumerate(pane.rows) if r.cells[0] == "AutoOffTime"
        )
        app._edit(expanded)
        assert expanded.overlay is not None
        assert "AutoOffTime" in expanded.overlay.subject


class TestAskingForAValue:
    def test_a_boolean_is_chosen_from_a_list(self, expanded: State) -> None:
        """`true`, `True`, `1`, `on`, `yes` — a person has no way of knowing which one
        this hub wants. A list removes the question."""
        window = app._question(
            title="t",
            subject="p",
            type_name="bool",
            choices=(),
            now="no",
            on_accept=lambda _: None,
            note="",
        )
        assert isinstance(window, overlay.Choose)
        assert window.options == ("yes", "no")
        assert window.cursor == 1, "the cursor stands on the current value"

    def test_a_number_has_its_range_written_out(self) -> None:
        window = app._question(
            title="t",
            subject="p",
            type_name="u16",
            choices=(),
            now="30",
            on_accept=lambda _: None,
            note="",
        )
        assert isinstance(window, overlay.Edit)
        assert "0…65535" in window.hint
        assert window.buffer == "30", "the current value is filled in, no need to retype it"

    def test_yes_and_no_come_back_as_booleans(self) -> None:
        saved: list[Any] = []
        window = app._question(
            title="t",
            subject="p",
            type_name="bool",
            choices=(),
            now="",
            on_accept=saved.append,
            note="",
        )
        assert isinstance(window, overlay.Choose)
        window.key(10)
        assert saved == [True]

    def test_a_text_comes_back_as_typed(self) -> None:
        """A display line: `yes` is a word and `30` two characters, not a boolean and a
        number (FEAT-12)."""
        saved: list[Any] = []
        window = app._question(
            title="t",
            subject="p",
            type_name="string",
            choices=(),
            now="",
            on_accept=saved.append,
            note="",
        )
        assert isinstance(window, overlay.Edit)
        assert "bytes" in window.hint
        window.on_accept("yes")
        window.on_accept("30")
        assert saved == ["yes", "30"]


class TestCommandPicker:
    def test_the_command_picker_skips_those_without_a_feature(self, expanded: State) -> None:
        expanded.enter()
        expanded.detail_cursor = _ep(expanded, 4)
        app.key(expanded, ord("c"))
        assert isinstance(expanded.overlay, overlay.Choose)
        assert expanded.overlay.options == ("On", "Off", "Toggle")
        assert "SetLevel" in expanded.overlay.note, "one sees it exists, but not here"

    def test_a_command_with_arguments_asks_for_them_with_ranges(self, expanded: State) -> None:
        from lagunhub.adapters.tui import actions
        from lagunhub.api import ArgView, CommandView

        endpoint = replace(
            EP4,
            commands=(
                CommandView(
                    name="SetLevel",
                    args=(ArgView(name="level", type="u16", lo=0, hi=65535),),
                ),
            ),
        )
        actions.send_command(expanded, "kitchen-relay", endpoint)
        assert isinstance(expanded.overlay, overlay.Choose)
        expanded.overlay.key(10)
        assert isinstance(expanded.overlay, overlay.Edit)
        assert "level: u16 0…65535" in expanded.overlay.hint
        assert expanded.overlay.buffer == "level=0"


class TestQuestionChain:
    """A window that opened the next one must not close it on its way out.

    `app.key` closed the window after confirmation — including when `on_accept` had
    already opened the next one. The effect: **no chain of questions worked**. `Device`
    commands have nothing but arguments, so not one of them even reached the bus; the
    binding wizard broke off at the first step.
    """

    def test_a_command_with_arguments_opens_a_second_window(self, expanded: State) -> None:
        expanded.enter()
        expanded.detail_cursor = _ep(expanded, 0)
        app.key(expanded, ord("c"))
        assert isinstance(expanded.overlay, overlay.Choose), "the command choice"

        app.key(expanded, 10)
        assert isinstance(expanded.overlay, overlay.Edit), "and then the question about arguments"
        assert expanded.overlay.buffer == "duration=0"

    def test_a_device_command_runs_to_the_end(self, expanded: State) -> None:
        expanded.enter()
        expanded.detail_cursor = _ep(expanded, 0)
        app.key(expanded, ord("c"))
        app.key(expanded, 10)
        app.key(expanded, 10)
        _run(expanded)
        assert ("command", ("kitchen-relay", 0, "Identify", {"duration": 0})) in _fake(
            expanded
        ).calls

    def test_the_binding_wizard_goes_through_every_step(self, state: State) -> None:
        state.section = Section.BINDINGS
        app.key(state, ord("+"))
        steps = ["source: node"]
        assert isinstance(state.overlay, overlay.Choose)
        state.overlay.cursor = list(state.overlay.options).index("hall-switch")

        app.key(state, 10)  # hall-switch
        _run(state)
        steps.append("source: endpoint")
        assert isinstance(state.overlay, overlay.Choose)
        state.overlay.cursor = next(
            i for i, o in enumerate(state.overlay.options) if o.startswith("ep4")
        )

        app.key(state, 10)  # ep4 KeyInput
        steps.append("event")
        assert isinstance(state.overlay, overlay.Choose)
        assert "Click" in state.overlay.options
        state.overlay.cursor = list(state.overlay.options).index("Click")

        app.key(state, 10)  # Click
        steps.append("target: node")
        assert isinstance(state.overlay, overlay.Choose)
        state.overlay.cursor = list(state.overlay.options).index("kitchen-relay")

        app.key(state, 10)  # kitchen-relay
        _run(state)
        steps.append("target: endpoint")
        assert isinstance(state.overlay, overlay.Choose)
        state.overlay.cursor = next(
            i for i, o in enumerate(state.overlay.options) if o.startswith("ep4")
        )

        app.key(state, 10)  # ep4 Output
        steps.append("command")
        assert isinstance(state.overlay, overlay.Choose)
        state.overlay.cursor = list(state.overlay.options).index("Toggle")

        app.key(state, 10)  # Toggle
        _run(state)
        assert len(steps) == 6

        write = next(c for c in _fake(state).calls if c[0] == "config_set")
        path, value, _ = write[1]
        assert path == "bindings"
        assert value[-1] == {
            "from": {
                "node": "hall-switch",
                "endpoint": 4,
                "type": "KeyInput",
                "event": "Click",
            },
            "to": {
                "node": "kitchen-relay",
                "endpoint": 4,
                "type": "Output",
                "command": "Toggle",
            },
        }

    def test_the_wizard_binds_a_report_to_a_write(self, state: State) -> None:
        """A reported attribute as the source, a writable one of the same type as the
        target — the binding that carries a sensor's value to a display field
        (zadania/zrobione/FEAT-13.md). Only same-type attributes are offered: the loader would
        refuse the rest."""
        state.section = Section.BINDINGS
        app.key(state, ord("+"))
        assert isinstance(state.overlay, overlay.Choose)
        state.overlay.cursor = list(state.overlay.options).index("kitchen-relay")
        app.key(state, 10)
        _run(state)

        assert isinstance(state.overlay, overlay.Choose)
        state.overlay.cursor = next(
            i for i, o in enumerate(state.overlay.options) if o.startswith("ep4")
        )
        app.key(state, 10)  # ep4 Output — no events, one reported attribute

        assert isinstance(state.overlay, overlay.Choose)
        assert state.overlay.options == ("report: OnOff",)
        app.key(state, 10)

        assert isinstance(state.overlay, overlay.Choose)
        state.overlay.cursor = list(state.overlay.options).index("hall-switch")
        app.key(state, 10)
        _run(state)

        assert isinstance(state.overlay, overlay.Choose)
        assert all(o.startswith("ep4") for o in state.overlay.options), "only a bool to write"
        app.key(state, 10)  # ep4 KeyInput

        assert isinstance(state.overlay, overlay.Choose)
        assert state.overlay.options == ("HoldEnabled",), "HoldThreshold is a u16"
        app.key(state, 10)
        _run(state)

        write = next(c for c in _fake(state).calls if c[0] == "config_set")
        path, value, _ = write[1]
        assert path == "bindings"
        assert value[-1] == {
            "from": {
                "node": "kitchen-relay",
                "endpoint": 4,
                "type": "Output",
                "attribute": "OnOff",
            },
            "to": {
                "node": "hall-switch",
                "endpoint": 4,
                "type": "KeyInput",
                "attribute": "HoldEnabled",
            },
        }


class TestNaming:
    def test_an_alias_is_stored_under_the_uid(self, expanded: State) -> None:
        app.key(expanded, ord("N"))
        assert isinstance(expanded.overlay, overlay.Edit)
        assert RELAY in expanded.overlay.subject
        expanded.overlay.buffer = "lamp"
        expanded.overlay.key(10)
        _run(expanded)
        assert ("config_set", (f"nodes/{RELAY}/alias", "lamp", "sha256:aaa")) in _fake(
            expanded
        ).calls

    def test_an_empty_name_is_refused(self, expanded: State) -> None:
        app.key(expanded, ord("n"))
        assert isinstance(expanded.overlay, overlay.Edit)
        expanded.overlay.buffer = "   "
        expanded.overlay.key(10)
        assert "an empty name" in expanded.status


class TestBindings:
    def test_without_aliases_the_wizard_says_what_to_do(self, state: State) -> None:
        state.section = Section.BINDINGS
        state.snapshot = replace(
            state.snapshot,
            nodes=(NodeSummary(uid=RELAY, alias="", name="no alias"),),
        )
        app.key(state, ord("+"))
        assert "give one with [N]" in state.status

    def test_removal_requires_retyping_the_alias(self, state: State) -> None:
        state.section = Section.BINDINGS
        state.snapshot = replace(
            state.snapshot,
            bindings=(
                BindingView(
                    source_uid="00" * 8,
                    source_alias="hall-switch",
                    source_endpoint=4,
                    targets=(TargetView(kind="COMMAND", endpoint=4, element=2),),
                ),
            ),
        )
        app.key(state, ord("-"))
        assert isinstance(state.overlay, overlay.Confirm)
        assert state.overlay.expect == "hall-switch"


class TestKeys:
    def test_q_ends_it(self, state: State) -> None:
        assert app.key(state, ord("q")) is False

    def test_back_at_the_very_top_does_NOT_end_it(self, state: State) -> None:
        """A key one navigates with must not close the view on one press too many.
        Quitting is on `q` and only there."""
        for _ in range(5):
            assert app.key(state, curses.KEY_LEFT) is True
        assert app.key(state, curses.KEY_BACKSPACE) is True
        assert app.key(state, 27) is True, "nor Esc"
        assert state.focus is Focus.NAV

    def test_side_arrows_go_deeper_and_tab_moves_between_sections(self, expanded: State) -> None:
        steps: list[Focus] = []
        app.key(expanded, curses.KEY_RIGHT)
        steps.append(expanded.focus)
        app.key(expanded, curses.KEY_LEFT)
        steps.append(expanded.focus)
        assert steps == [Focus.DETAIL, Focus.LIST]

        app.key(expanded, ord("\t"))
        assert expanded.section is Section.BINDINGS

    def test_back_from_the_list_lands_on_the_section(self, state: State) -> None:
        """Every list is driven with arrows — the one with the sections too."""
        steps: list[Focus] = [state.focus]
        app.key(state, curses.KEY_LEFT)
        steps.append(state.focus)
        assert steps == [Focus.LIST, Focus.NAV]

        app.key(state, curses.KEY_DOWN)
        assert state.section is Section.BINDINGS, "the arrows walk the sections too"

    def test_esc_clears_the_filter_first(self, state: State) -> None:
        state.filter = "relay"
        assert app.key(state, 27) is True
        assert state.filter == ""

    def test_space_pauses_the_feed(self, state: State) -> None:
        app.key(state, ord(" "))
        assert state.feed.paused is True

    def test_a_toggle_without_an_output_says_so(self, state: State) -> None:
        app.key(state, ord("t"))
        assert "nothing here that can be driven" in state.status


def _ep(state: State, number: int) -> int:
    return next(i for i, e in enumerate(state.detail_rows()) if e.number == number)


def _into_ep4(state: State) -> None:
    """Go into `ep4`’s attributes. Separate, because the first on the list is now
    `ep0 Device` — system endpoints have been in the model since it turned out they were
    missing."""
    state.enter()
    state.detail_cursor = _ep(state, 4)
    state.enter()


def _fake(state: State) -> FakeHub:
    assert isinstance(state.hub, FakeHub)
    return state.hub


def _run(state: State) -> None:
    """Carry out what the view handed the worker thread — in a test we do it outright."""
    while not state.worker._queue.empty():
        task = state.worker._queue.get_nowait()
        if task is not None:
            task()


def _index(state: State, name: str) -> int:
    return next(i for i, a in enumerate(state.detail_rows()) if a.name == name)


class TestStrangers:
    """A node found at an address the hub did not know — 06-cykl-zycia-urzadzenia.md §2.3."""

    def joining(self, state: State, *rows: JoinView) -> State:
        state.worker._on_done = lambda _: None
        state.section = Section.JOINING
        state.snapshot = replace(
            state.snapshot, joining=CommissioningView(accepting=False, known=rows)
        )
        return state

    def test_y_admits_a_pending_node_with_an_address(self, state: State) -> None:
        self.joining(state, JoinView(0xAABBCCDD, "CONTROL", address=0x040, state="PENDING"))
        app.key(state, ord("y"))
        _run(state)
        assert ("admit", (0xAABBCCDD,)) in _fake(state).calls

    def test_x_resets_only_after_the_short_id_is_retyped(self, state: State) -> None:
        self.joining(state, JoinView(0xAABBCCDD, "CONTROL", address=0x040, state="PENDING"))
        app.key(state, ord("X"))
        _run(state)
        assert not any(call[0] == "factory_reset" for call in _fake(state).calls)
        for letter in "AABBCCDD":
            app.key(state, ord(letter))
        app.key(state, 10)
        _run(state)
        assert ("factory_reset", (0xAABBCCDD,)) in _fake(state).calls

    def test_a_node_waiting_to_join_is_not_admitted_this_way(self, state: State) -> None:
        """Without an address the accepting mode is its road in, not `y`."""
        self.joining(state, JoinView(0xAABBCCDD, "CONTROL", state="PENDING"))
        app.key(state, ord("y"))
        _run(state)
        assert not any(call[0] == "admit" for call in _fake(state).calls)


class TestNodeLifecycle:
    """Removing, blocking, enumerating again — 06-cykl-zycia-urzadzenia.md §6, §8, §3.3."""

    def nodes(self, state: State) -> State:
        state.worker._on_done = lambda _: None
        state.section = Section.NODES
        return state

    def test_x_asks_once_and_enter_removes(self, state: State) -> None:
        self.nodes(state)
        app.key(state, ord("X"))
        assert isinstance(state.overlay, overlay.Choose)
        _run(state)
        assert not any(call[0] == "remove" for call in _fake(state).calls)
        app.key(state, 10)  # the first option — wait for the node
        _run(state)
        assert ("remove", ("kitchen-relay", False)) in _fake(state).calls, "no second window"

    def test_esc_removes_nothing(self, state: State) -> None:
        self.nodes(state)
        app.key(state, ord("X"))
        app.key(state, 27)
        _run(state)
        assert state.overlay is None
        assert not any(call[0] == "remove" for call in _fake(state).calls)

    def test_after_removal_the_list_drops_the_node(self, state: State) -> None:
        """The node under the cursor is the one just removed — asking the hub about it
        must not stop the whole view from refreshing (it did: the list kept the removed
        node and every refresh failed again on it)."""
        self.nodes(state)
        app.key(state, ord("X"))
        app.key(state, 10)
        _run(state)

        assert [n.alias for n in state.list_rows()] == ["hall-switch"]
        assert state.snapshot.error == ""
        assert state.snapshot.node is None or state.snapshot.node.alias == "hall-switch"

    def test_up_works_at_once_when_the_list_got_shorter(self, state: State) -> None:
        """A cursor left past the end shows the last row; the first `↑` has to leave it,
        not only bring the number back in range."""
        self.nodes(state)
        state.cursor[Section.NODES] = 5  # the list had six rows a moment ago
        assert state.selected().alias == "hall-switch"
        app.key(state, curses.KEY_UP)
        assert state.selected().alias == "kitchen-relay"

    def test_force_is_the_second_option(self, state: State) -> None:
        self.nodes(state)
        app.key(state, ord("X"))
        app.key(state, 258)  # KEY_DOWN
        app.key(state, 10)
        _run(state)
        assert ("remove", ("kitchen-relay", True)) in _fake(state).calls

    def test_x_in_joining_still_resets_a_stranger(self, state: State) -> None:
        TestStrangers().joining(
            state, JoinView(0xAABBCCDD, "CONTROL", address=0x040, state="PENDING")
        )
        app.key(state, ord("X"))
        assert isinstance(state.overlay, overlay.Confirm)

    def test_e_enumerates(self, state: State) -> None:
        self.nodes(state)
        app.key(state, ord("E"))
        _run(state)
        assert ("enumerate", ("kitchen-relay",)) in _fake(state).calls

    def test_capital_g_reads_the_whole_node_from_the_list(self, state: State) -> None:
        """`G` — a capital `g`: everything instead of one attribute
        (zadania/zrobione/FEAT-06.md)."""
        self.nodes(state)
        app.key(state, ord("G"))
        _run(state)
        assert ("read_all", ("kitchen-relay", None)) in _fake(state).calls

    def test_capital_g_inside_an_endpoint_reads_only_it(self, expanded: State) -> None:
        state = expanded
        _into_ep4(state)
        app.key(state, ord("G"))
        _run(state)
        assert ("read_all", ("kitchen-relay", 4)) in _fake(state).calls

    def test_the_title_says_a_readout_is_running(self, expanded: State) -> None:
        """The view polls and does not listen, so progress comes from the node view
        (R10)."""
        state = expanded
        node = state.snapshot.node
        assert node is not None
        assert "reading" not in panes.details(state).title
        state.snapshot = replace(state.snapshot, node=replace(node, reading=42))
        assert "reading, 42 left" in panes.details(state).title
        _into_ep4(state)
        assert "reading, 42 left" in panes.details(state).title

    def test_capital_g_is_offered(self, state: State) -> None:
        self.nodes(state)
        assert ("G", "read everything") in panes.context_keys(state)

    def test_b_blocks_and_on_a_blocked_one_unblocks(self, state: State) -> None:
        self.nodes(state)
        app.key(state, ord("B"))
        _run(state)
        assert ("block", ("kitchen-relay",)) in _fake(state).calls

        rows = tuple(replace(n, blocked=True) for n in state.snapshot.nodes)
        state.snapshot = replace(state.snapshot, nodes=rows)
        app.key(state, ord("B"))
        _run(state)
        assert ("unblock", ("kitchen-relay",)) in _fake(state).calls

    def test_b_blocks_a_pending_stranger_by_uid(self, state: State) -> None:
        TestStrangers().joining(
            state,
            JoinView(0xAABBCCDD, "CONTROL", address=0x040, state="PENDING", uid=RELAY),
        )
        app.key(state, ord("B"))
        _run(state)
        assert ("block", (RELAY,)) in _fake(state).calls

    def test_the_keys_are_offered_on_a_node(self, state: State) -> None:
        self.nodes(state)
        keys = dict(panes.context_keys(state))
        assert keys["X"] == "remove" and keys["E"] == "enumerate" and keys["B"] == "block"


class TestBuses:
    """The `Buses` section — the hardware under each bus from the file
    (zadania/FEAT-01.md, step 6; can-drivers/docs/MAKIETY-MAGISTRALE.md, screens 1, 3, 12)."""

    def buses(self, state: State) -> State:
        state.worker._on_done = lambda _: None
        state.section = Section.BUSES
        state.reload()
        return state

    def test_the_section_sits_between_issues_and_joining(self) -> None:
        sections = list(Section)
        assert sections.index(Section.BUSES) == sections.index(Section.ISSUES) + 1
        assert sections.index(Section.JOINING) == sections.index(Section.BUSES) + 1

    def test_hardware_is_asked_only_while_the_section_is_open(self, state: State) -> None:
        """Every look runs `ip` on the hub — not every two seconds for nobody."""
        state.section = Section.NODES
        state.reload()
        assert not any(call[0] == "buses" for call in _fake(state).calls)
        self.buses(state)
        assert state.snapshot.buses

    def test_the_list(self, state: State) -> None:
        self.buses(state)
        pane = panes.items(state)
        assert pane.columns[0] == "NAME"
        (row,) = pane.rows
        assert row.cells == (
            "ground-floor",
            "socketcan",
            "can0",
            "mcp251xfd",
            "500 kbit/s",
            "yes",
            "1",
            "DRIFT 1",
        )
        assert row.colour == panes.BAD

    def test_the_detail_shows_what_will_run(self, state: State) -> None:
        self.buses(state)
        manual = ChangeView("interface", "missing", "can0", "`dmesg`", manual=True)
        bus = replace(state.snapshot.buses[0], changes=(*state.snapshot.buses[0].changes, manual))
        state.snapshot = replace(state.snapshot, buses=(bus,))
        note = panes.details(state).note
        bitrate = next(line for line in note if line.startswith("bitrate"))
        assert bitrate.split() == ["bitrate", "unset", "500", "kbit/s"]
        interface = next(line for line in note if line.startswith("interface"))
        assert interface.split() == ["interface", "missing", "can0", "BY", "HAND"]
        assert "  ip link set can0 type can bitrate 500000" in note
        assert "  `dmesg`" in note, "the manual one says what to do"

    def test_an_error_is_shown_not_hidden(self, state: State) -> None:
        self.buses(state)
        broken = BusSetupView(
            name="first-floor",
            kind="socketcan",
            channel="can1",
            bitrate=500_000,
            state="error",
            error="no /sys/class/net: this is not Linux",
        )
        state.snapshot = replace(state.snapshot, buses=(broken,))
        (row,) = panes.items(state).rows
        assert row.cells[-1] == "ERROR"
        assert "no /sys/class/net: this is not Linux" in panes.details(state).note

    def test_a_failed_link_outranks_the_plan(self, state: State) -> None:
        """The running hub's word on a bus first (zadania/zrobione/BUG-04.md)."""
        self.buses(state)
        fault = "bus ground-floor (can0) failed — it is down, and not by the hub"
        bus = replace(state.snapshot.buses[0], state="ok", changes=(), link="failure", fault=fault)
        state.snapshot = replace(state.snapshot, buses=(bus,))
        (row,) = panes.items(state).rows
        assert row.cells[-1] == "FAILURE"
        assert row.colour == panes.BAD
        assert panes.details(state).note[0] == fault

    def test_u_reconciles_only_after_the_name_is_retyped(self, state: State) -> None:
        """Reconciling takes the bus down for a moment — the weight of a factory reset."""
        self.buses(state)
        app.key(state, ord("u"))
        assert isinstance(state.overlay, overlay.Confirm)
        _run(state)
        assert ("bus_apply", ("socketcan", "ground-floor")) not in _fake(state).calls
        for letter in "ground-floor":
            app.key(state, ord(letter))
        app.key(state, 10)
        _run(state)
        assert ("bus_apply", ("socketcan", "ground-floor")) in _fake(state).calls
        assert state.snapshot.buses[0].state == "ok"

    def test_nothing_to_reconcile_nothing_asked(self, state: State) -> None:
        self.buses(state)
        state.snapshot = replace(
            state.snapshot, buses=(replace(state.snapshot.buses[0], state="ok", changes=()),)
        )
        app.key(state, ord("u"))
        assert state.overlay is None
        assert panes.context_keys(state) == ()

    def test_the_key_is_listed_when_there_is_something_to_do(self, state: State) -> None:
        self.buses(state)
        assert ("u", "reconcile — the bus goes down for a moment") in panes.context_keys(state)


class TestWhichAttributesAreShown:
    """Every attribute the endpoint can have, known or not (FEAT-12): a display field's
    `Content` before any read, and `Value`/`Text`, which start empty and are written
    only with `w` — without a row there was nothing to stand on."""

    @staticmethod
    def field(**overrides: Any) -> Any:
        from lagunhub.api import AttributeView

        base = {"id": 0, "name": "x", "type": "u8", "access": "RO"}
        return AttributeView(**{**base, **overrides})

    def test_unknown_and_writable_rows_are_there(self) -> None:
        from lagunhub.api import EndpointView

        endpoint = EndpointView(
            number=4,
            attributes=(
                self.field(id=0, name="Content", type="enum8"),
                self.field(id=2, name="Value", type="<Q>", access="RW"),
                self.field(id=4, name="Text", type="string", access="RW", available=False),
            ),
        )
        assert [a.name for a in panes.worth_showing(endpoint)] == ["Content", "Value"], (
            "Text belongs to a feature this field lacks — `w` would only be refused"
        )

    def test_a_value_the_endpoint_should_not_have_is_still_shown(self) -> None:
        from lagunhub.api import EndpointView

        endpoint = EndpointView(
            number=4, attributes=(self.field(name="Text", available=False, wanted="hi"),)
        )
        assert [a.name for a in panes.worth_showing(endpoint)] == ["Text"]
