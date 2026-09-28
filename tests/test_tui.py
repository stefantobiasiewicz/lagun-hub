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
    NodeSummary,
    TargetView,
    TrafficView,
)
from tests.test_shell import EP4, RELAY, FakeHub


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
        from tests.test_shell import TRAFFIC

        command, response = (
            panes.traffic_row(TRAFFIC[0])[0],
            panes.traffic_row(TRAFFIC[1])[0],
        )
        assert "→" in command and "COMMAND" in command and "tid=1" in command
        assert "0x001 ep4" in command and "Toggle" in command
        assert "RESPONSE" in response and "OK" in response and "3.1 ms" in response

    def test_a_refusal_is_red(self) -> None:
        from tests.test_shell import TRAFFIC

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
