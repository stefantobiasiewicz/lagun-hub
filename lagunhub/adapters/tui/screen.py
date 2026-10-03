"""Drawing — the frame, two columns, a details panel, the traffic feed.

All of `curses` lives here. The content arrives finished from [`panes.py`](panes.py),
so this file knows nothing about the hub and can be checked with a mock screen.

The layout is described in [`ARCHITEKTURA.md`](../../../ARCHITEKTURA.md) §10.3.3:
navigation has a **fixed width and always shows every section**, while the right column
splits into a list at the top and the details of the selected item below. Nothing leaves
the eye — that was the main flaw of the first version, which descended into depth.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from contextlib import suppress
from typing import Any, Final

from lagunhub.adapters.tui import panes
from lagunhub.adapters.tui.panes import BAD, DIM, GOOD, HEADER, NORMAL, SELECTED, Pane
from lagunhub.adapters.tui.state import Focus, Section, State

NAV_W: Final = 20
"""The width of the section column. It fits `▸ Joining` and a counter **with a gap** —
any tighter and the digits blended into the name."""
MIN_H: Final = 12
MIN_W: Final = 50

_PAIRS: dict[int, int] = {}
"""Prepared colour attributes. Empty means "we draw without colours" — as on a terminal
that has none, and likewise in a test, where `curses` is not initialised."""


def pair(number: int) -> int:
    return _PAIRS.get(number, 0)


def init_colors() -> None:
    import curses

    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    for number, (fg, bg) in {
        NORMAL: (-1, -1),
        SELECTED: (curses.COLOR_BLACK, curses.COLOR_CYAN),
        HEADER: (curses.COLOR_BLACK, curses.COLOR_WHITE),
        DIM: (curses.COLOR_BLUE, -1),
        BAD: (curses.COLOR_RED, -1),
        GOOD: (curses.COLOR_GREEN, -1),
    }.items():
        curses.init_pair(number, fg, bg)
        _PAIRS[number] = curses.color_pair(number)


def draw(stdscr: Any, state: State) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < MIN_H or width < MIN_W:
        _put(stdscr, 0, 0, f"window too small — {MIN_W}x{MIN_H} needed", width)
        stdscr.refresh()
        return

    preview = max(3, (height - 4) // 3)
    body = height - preview - 4
    right_w = width - NAV_W - 2

    _frame(stdscr, state, height, width, body, preview)
    _nav(stdscr, state, 1, body)
    _right(stdscr, state, 1, NAV_W + 1, body, right_w)
    _feed(stdscr, state, 2 + body, width, preview)
    _key_bar(stdscr, state, height - 1, width)
    if state.overlay is not None:
        _overlay(stdscr, state.overlay, height, width)
    stdscr.refresh()


# --- the frame and the bars -------------------------------------------------------


def _frame(stdscr: Any, state: State, h: int, w: int, body: int, preview: int) -> None:
    info = state.snapshot.info
    if state.snapshot.error:
        title = f"─ LagunBus ─ ✖ {state.snapshot.error} "
        colour = BAD
    else:
        status = (
            f"nodes {info.ready}/{info.nodes} ─ issues {info.issues} " if info else "connecting… "
        )
        title = f"─ LagunBus ─ {', '.join(info.buses) if info else '—'} ● ─ {status}"
        colour = HEADER
    clock = f"─ {time.strftime('%H:%M:%S')} ─"
    top_bar = title + "─" * max(0, w - 2 - len(title) - len(clock)) + clock
    _put(stdscr, 0, 0, "┌" + top_bar[: w - 2] + "┐", w, pair(colour))
    for y in range(1, 1 + body):
        _put(stdscr, y, 0, "│", 1)
        _put(stdscr, y, w - 1, "│", 1)

    feed = state.feed
    if not feed.connected:
        bar = "─ Traffic on the bus ─ DISCONNECTED "
    else:
        status = "PAUSED" if feed.paused else f"{feed.rate:.0f} frames/s"
        lost = f"─ lost {feed.dropped} " if feed.dropped else ""
        bar = f"─ Traffic on the bus ─ {status} {lost}─ [space] pause "
    _put(stdscr, 1 + body, 0, "├" + (bar + "─" * w)[: w - 2] + "┤", w, pair(DIM))
    for y in range(2 + body, 2 + body + preview):
        _put(stdscr, y, 0, "│", 1)
        _put(stdscr, y, w - 1, "│", 1)
    _put(stdscr, h - 2, 0, "└" + "─" * (w - 2) + "┘", w)


def _nav(stdscr: Any, state: State, top: int, height: int) -> None:
    counters = {
        Section.NODES: len(state.snapshot.nodes),
        Section.BINDINGS: sum(len(b.targets) for b in state.snapshot.bindings),
        Section.DIFF: sum(1 for r in state.snapshot.diff if r.state != "match"),
        Section.ISSUES: len(state.snapshot.issues),
        Section.BUSES: len(state.snapshot.buses),
        Section.JOINING: len(state.snapshot.joining.known) if state.snapshot.joining else 0,
        Section.HEALTH: 0,
    }
    for i, section in enumerate(Section):
        if i >= height:
            break
        chosen = section is state.section
        counter = str(counters[section]) if counters[section] else ""
        left_text = f" {'▸' if chosen else ' '} {section.value}"
        text = _pad(left_text, NAV_W - len(counter) - 2) + counter + "  "
        # Strong highlight where the cursor is; weaker where it was.
        colour = (SELECTED if state.focus is Focus.NAV else HEADER) if chosen else NORMAL
        _put(stdscr, top + i, 1, text, NAV_W, pair(colour))


def _right(stdscr: Any, state: State, top: int, left: int, height: int, width: int) -> None:
    upper = panes.items(state)
    lower = panes.details(state)
    context_keys = panes.context_keys(state)
    if not lower.rows and not lower.note and not lower.title:
        list_height = height
    else:
        list_height = _split(upper, lower, bool(context_keys), height)

    _table(
        stdscr,
        upper,
        top,
        left,
        list_height,
        width,
        state.cursor[state.section],
        state.focus is Focus.LIST,
    )
    if list_height >= height:
        return

    y = top + list_height
    title = lower.title or ""
    _put(stdscr, y, left, _subtitle(title, width - 1), width, pair(DIM))
    remaining = height - list_height - 1
    action_rows = 1 if context_keys else 0
    if lower.note:
        for i, line in enumerate(lower.note[: remaining - action_rows]):
            _put(stdscr, y + 1 + i, left, " " + line, width)
    elif lower.rows or lower.empty:
        _table(
            stdscr,
            lower,
            y + 1,
            left,
            remaining - action_rows,
            width,
            state.detail_cursor,
            state.focus is Focus.DETAIL,
        )
    if context_keys:
        _put(stdscr, top + height - 1, left, _action_line(context_keys, width), width, pair(DIM))


def _action_line(context_keys: Sequence[tuple[str, str]], width: int) -> str:
    """The keys that fit — and when not all do, the rest as **bare characters**.

    A cut in mid-word ("[N] a") looks like a fault rather than like a lack of room.
    """
    whole = ["  " + " ".join(f"[{k}] {description}" for k, description in context_keys)]
    if len(whole[0]) <= width:
        return whole[0]
    fitted: list[str] = []
    for i, (k, description) in enumerate(context_keys):
        attempt = "  " + " ".join([*fitted, f"[{k}] {description}"])
        rest = " ".join(f"[{key}]" for key, _ in context_keys[i + 1 :])
        if len(attempt) + len(rest) + 1 > width:
            bare = " ".join(f"[{key}]" for key, _ in context_keys[i:])
            return "  " + " ".join(fitted) + " " + bare
        fitted.append(f"[{k}] {description}")
    return "  " + " ".join(fitted)


def _split(upper: Pane, lower: Pane, has_actions: bool, height: int) -> int:
    """How many rows the list gets.

    **Surplus goes to the list, not to the details.** The details panel needs as much
    as it has content and not one row more; the list can always show another node. The
    first version split the screen in half and the list choked at three items, with ten
    empty rows beneath it.
    """
    list_need = (len(upper.rows) + (1 if upper.columns else 0)) or 1
    detail_content = len(lower.note) or (len(lower.rows) + (1 if lower.columns else 0)) or 1
    detail_need = 1 + detail_content + (1 if has_actions else 0)
    if list_need + detail_need <= height:
        return height - detail_need
    return max(3, min(list_need, height * 2 // 3))


def _table(
    stdscr: Any,
    pane: Pane,
    top: int,
    left: int,
    height: int,
    width: int,
    cursor: int,
    active: bool,
) -> None:
    if not pane.rows:
        _put(stdscr, top, left, " " + pane.empty, width, pair(DIM))
        return
    widths = _widths(pane, width - 2)
    if pane.columns:
        _put(
            stdscr,
            top,
            left,
            " " + _row(pane.columns, widths, pane.aligns),
            width,
            pair(DIM),
        )
        top += 1
        height -= 1
    visible = max(1, height)
    start = max(0, min(cursor - visible // 2, len(pane.rows) - visible))
    for i in range(visible):
        index = start + i
        if index >= len(pane.rows):
            break
        row = pane.rows[index]
        highlighted = index == cursor and active
        marker = "▸" if index == cursor else " "
        text = marker + _row(row.cells, widths, pane.aligns)
        _put(
            stdscr,
            top + i,
            left,
            text,
            width,
            pair(SELECTED) if highlighted else pair(row.colour),
        )


def _feed(stdscr: Any, state: State, top: int, width: int, height: int) -> None:
    feed = state.feed
    if not feed.items:
        text = " — the bus is quiet —" if feed.connected else " — feed disconnected —"
        _put(stdscr, top, 1, text, width - 2, pair(DIM))
        return
    for i, item in enumerate(feed.items[-height:]):
        text, colour = panes.traffic_row(item)
        _put(stdscr, top + i, 1, " " + text, width - 2, pair(colour))


def _key_bar(stdscr: Any, state: State, row: int, width: int) -> None:
    if state.status:
        _put(
            stdscr,
            row,
            0,
            " " + state.status,
            width,
            pair(BAD if state.status.startswith("!") else DIM),
        )
        return
    parts = ["↑↓ select", "→ deeper", "← back", "⇥ section"]
    if state.filter:
        parts.append(f"/ filter: {state.filter}")
    parts += ["r refresh", "? help", "q quit"]
    _put(stdscr, row, 0, " " + "   ".join(parts), width, pair(HEADER))


# --- the window on top ------------------------------------------------------------


def _overlay(stdscr: Any, overlay: Any, h: int, w: int) -> None:
    lines: list[str] = list(overlay.lines())
    col_w = min(w - 6, max(len(line) for line in lines) + 4)
    hgt = len(lines) + 2
    top = max(0, (h - hgt) // 2)
    left = max(0, (w - col_w) // 2)
    cap = f"─ {overlay.title} "
    _put(
        stdscr,
        top,
        left,
        "┌" + cap + "─" * max(0, col_w - 2 - len(cap)) + "┐",
        col_w,
        pair(HEADER),
    )
    for i, line in enumerate(lines):
        _put(stdscr, top + 1 + i, left, "│ " + _pad(line, col_w - 4) + " │", col_w)
    _put(stdscr, top + hgt - 1, left, "└" + "─" * (col_w - 2) + "┘", col_w)


# --- small change -----------------------------------------------------------------


def _widths(pane: Pane, available: int) -> list[int]:
    columns = len(pane.columns) or (len(pane.rows[0].cells) if pane.rows else 0)
    if not columns:
        return []
    col_w = [len(pane.columns[i]) if i < len(pane.columns) else 0 for i in range(columns)]
    for row in pane.rows:
        for i, cell in enumerate(row.cells[:columns]):
            col_w[i] = max(col_w[i], len(cell))
    excess = sum(col_w) + 2 * (columns - 1) - available
    while excess > 0:
        widest = col_w.index(max(col_w))
        if col_w[widest] <= 4:
            break
        how_many = min(excess, col_w[widest] - 4)
        col_w[widest] -= how_many
        excess -= how_many
    return col_w


def _row(cells: Sequence[str], widths: Sequence[int], aligns: Sequence[str]) -> str:
    parts = []
    for i, how_many in enumerate(widths):
        cell = cells[i] if i < len(cells) else ""
        truncated = cell if len(cell) <= how_many else cell[: max(1, how_many - 1)] + "…"
        sign = aligns[i] if i < len(aligns) else "<"
        parts.append(truncated.rjust(how_many) if sign == ">" else truncated.ljust(how_many))
    return "  ".join(parts).rstrip()


def _pad(text: str, how_many: int) -> str:
    return text.ljust(how_many)[:how_many] if len(text) > how_many else text.ljust(how_many)


def _put(stdscr: Any, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    """Printing with truncation. `curses` throws on the last character of the last row."""
    import curses

    height, full_w = stdscr.getmaxyx()
    if y >= height or x >= full_w or y < 0:
        return
    clipped = text[: max(0, min(width, full_w - x))]
    if y == height - 1 and x + len(clipped) >= full_w:
        clipped = clipped[: full_w - x - 1]
    with suppress(curses.error):
        stdscr.addstr(y, x, clipped, attr)


def _subtitle(title: str, how_many: int) -> str:
    text = f"── {title} " if title else "──"
    return (text + "─" * how_many)[:how_many]


__all__ = ["MIN_H", "MIN_W", "NAV_W", "draw", "init_colors", "pair"]
