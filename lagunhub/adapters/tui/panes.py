"""The panes’ content — **without a single `curses` call**.

That makes it possible to check by test what is easiest to get wrong: which columns, in
what order, what is dimmed and what is red. Drawing receives finished rows and deals
only with characters in a terminal ([`screen.py`](screen.py)).

The rule for this whole layer: **"I do not know" does not look like "zero"**. The hub
does not poll nodes after they join and restores state from reports
(01-model-danych.md §6), so until a report arrives the honest answer is a dash.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Final

from lagunhub.adapters.tui.state import Focus, Section, State
from lagunhub.api import (
    AttributeView,
    BindingView,
    BusSetupView,
    BusView,
    DiffRow,
    EndpointView,
    JoinView,
    NodeSummary,
    NodeView,
    Plain,
    TargetView,
    TrafficView,
)

MISSING: Final = "—"

NORMAL: Final = 1
SELECTED: Final = 2
HEADER: Final = 3
DIM: Final = 4
BAD: Final = 5
GOOD: Final = 6

VERDICTS: Final[dict[str, str]] = {
    "match": "agrees",
    "mismatch": "DRIFT",
    "unknown": "NOT READ",
    "missing": "NO NODE",
}

KIND_LABELS: Final[dict[str, str]] = {
    "ALARM": "ALARM",
    "EVENT": "EVENT",
    "COMMAND": "COMMAND",
    "GROUP_COMMAND": "GROUP CMD",
    "WRITE": "WRITE",
    "READ": "READ",
    "RESPONSE": "RESPONSE",
    "REPORT": "REPORT",
    "LONG_RESPONSE": "LONG RESP",
    "LONG_WRITE": "LONG WRITE",
    "QUERY": "QUERY",
    "HUB_RESPONSE": "HUB RESP",
    "JOIN": "JOIN",
}
"""Shortened names for the column. The core gives the registry name; fitting it into the
width is the view’s business."""


@dataclass(frozen=True, slots=True)
class Row:
    cells: tuple[str, ...]
    colour: int = NORMAL


@dataclass(frozen=True, slots=True)
class Pane:
    """One list: a column header and rows. `title` is filled in only by the details pane.

    **`items` runs parallel to `rows`, and that matters more than it looks.** A pane can
    filter rows out or skip them; were the state to count positions independently, the
    cursor would point at something other than what is visible — and `e` would edit an
    attribute other than the highlighted one. So what is visible and what is selected
    come from one place: here.
    """

    columns: tuple[str, ...] = ()
    rows: tuple[Row, ...] = ()
    items: tuple[Any, ...] = ()
    title: str = ""
    note: tuple[str, ...] = ()
    """Prose under the table — an explanation, not data."""

    actions: tuple[tuple[str, str], ...] = ()
    """`[t] Toggle` — a key and what it does. It changes with what is selected."""

    empty: str = ""
    aligns: tuple[str, ...] = field(default_factory=tuple)


# --- the list ---------------------------------------------------------------------


def items(state: State) -> Pane:
    pane = _LISTS[state.section](state)
    if not state.filter:
        return pane
    needle = state.filter.lower()
    remain = [i for i, r in enumerate(pane.rows) if any(needle in c.lower() for c in r.cells)]
    return replace(
        pane,
        rows=tuple(pane.rows[i] for i in remain),
        items=tuple(pane.items[i] for i in remain if i < len(pane.items)),
        empty=f'nothing matches "{state.filter}" — [/] changes it, Esc clears it',
    )


def _nodes(state: State) -> Pane:
    return Pane(
        columns=("ALIAS", "NAME", "ADDR", "STATE", "HEARD", "EP"),
        aligns=("<", "<", "<", "<", ">", ">"),
        rows=tuple(
            Row(
                (
                    n.alias or n.uid,
                    n.name or MISSING,
                    _addr(n.address),
                    n.state,
                    _age(n.silent_for),
                    str(n.endpoints) if n.endpoints else MISSING,
                ),
                GOOD if n.address else DIM,
            )
            for n in state.snapshot.nodes
        ),
        items=tuple(state.snapshot.nodes),
        empty="the installation is empty — open joining with [a]",
    )


def _bindings(state: State) -> Pane:
    return Pane(
        columns=("SOURCE", "TARGET", "STATE"),
        rows=tuple(
            Row(
                (
                    b.label or f"{b.source_alias} ep{b.source_endpoint}",
                    t.label or f"{t.alias} ep{t.endpoint}",
                    "works" if t.active else "TARGET HAS NO ADDRESS",
                ),
                GOOD if t.active else BAD,
            )
            for b in state.snapshot.bindings
            for t in b.targets
        ),
        items=tuple((b, t) for b in state.snapshot.bindings for t in b.targets),
        empty="there are no bindings — [+] adds one",
    )


def _diff(state: State) -> Pane:
    return Pane(
        columns=("NODE", "EP", "ATTRIBUTE", "IN THE FILE", "ON THE NODE", "STATE"),
        rows=tuple(
            Row(
                (
                    r.alias or r.uid,
                    f"ep{r.endpoint}",
                    r.attribute,
                    _val(r.wanted, r.wanted_label),
                    _val(r.actual, r.actual_label),
                    VERDICTS.get(r.state, r.state),
                ),
                {"match": GOOD, "mismatch": BAD}.get(r.state, DIM),
            )
            for r in state.snapshot.diff
        ),
        items=tuple(state.snapshot.diff),
        empty="there is nothing to reconcile",
    )


def _issues(state: State) -> Pane:
    return Pane(
        columns=("WHAT NEEDS THE OPERATOR'S DECISION",),
        rows=tuple(Row((u,), BAD) for u in state.snapshot.issues),
        items=tuple(state.snapshot.issues),
        empty="nothing needs attention",
    )


def _joining(state: State) -> Pane:
    view = state.snapshot.joining
    return Pane(
        columns=("SHORTID", "CLASS", "ADDRESS", "STATE", "UID"),
        rows=tuple(
            Row(
                (
                    f"0x{r.short_id:08X}",
                    r.node_class,
                    _addr(r.address),
                    r.state,
                    r.uid or MISSING,
                ),
                GOOD if r.uid else DIM,
            )
            for r in (view.known if view else ())
        ),
        items=tuple(view.known if view else ()),
        empty="nobody has announced yet",
    )


def _health(state: State) -> Pane:
    return Pane(empty="", rows=(), columns=())


def _buses(state: State) -> Pane:
    """Every bus in `hub.yaml` and the hardware under it (zadania/FEAT-01.md).

    `OPEN` says whether the hub has a socket on it in this run — a bus left out at start
    is here too, because that is where a person has to look.
    """
    return Pane(
        columns=("NAME", "KIND", "CHANNEL", "HARDWARE", "BITRATE", "OPEN", "NODES", "CONFIG"),
        aligns=("<", "<", "<", "<", ">", "<", ">", "<"),
        rows=tuple(
            Row(
                (
                    b.name,
                    b.kind,
                    b.channel,
                    b.hardware or MISSING,
                    f"{b.bitrate // 1000} kbit/s",
                    "yes" if b.open else "NO",
                    str(b.nodes),
                    _bus_state(b),
                ),
                GOOD if b.state == "ok" and b.open and b.link in ("", "up") else BAD,
            )
            for b in state.snapshot.buses
        ),
        items=tuple(state.snapshot.buses),
        empty="no buses in hub.yaml",
    )


def _bus_state(bus: BusSetupView) -> str:
    # The running hub's word first: a bus that failed is that, whatever its plan says.
    if bus.link in _LINK_ALARM:
        return _LINK_ALARM[bus.link]
    if bus.state == "ok":
        return "✓"
    if bus.state == "error":
        return "ERROR"
    automatic = sum(1 for change in bus.changes if not change.manual)
    by_hand = len(bus.changes) - automatic
    return f"DRIFT {automatic}" if automatic else f"BY HAND {by_hand}"


_LINK_ALARM: Final = {"failure": "FAILURE", "gave-up": "GAVE UP", "planned": "APPLYING"}
"""The watch's states worth a word in a list (zadania/zrobione/BUG-04.md); `up` and a bus not
watched say nothing."""


_LISTS: Final[dict[Section, Callable[[State], Pane]]] = {
    Section.NODES: _nodes,
    Section.BINDINGS: _bindings,
    Section.DIFF: _diff,
    Section.ISSUES: _issues,
    Section.BUSES: _buses,
    Section.JOINING: _joining,
    Section.HEALTH: _health,
}


# --- details ----------------------------------------------------------------------


def details(state: State) -> Pane:
    return _DETAILS[state.section](state)


def _node_detail(state: State) -> Pane:
    node = state.snapshot.node
    selected = state.selected()
    if node is None:
        if isinstance(selected, NodeSummary) and not selected.address:
            return Pane(
                title=f"{selected.alias or selected.uid} · configured, absent",
                note=(
                    "This node is in the file, but the hub has not seen it yet.",
                    "Either it is not connected, or the Uid in the file is wrong.",
                ),
            )
        return Pane(note=("…",))
    if state.endpoint is not None:
        return _attributes(state, node)
    return Pane(
        title=f"{node.alias or node.uid} · {node.name or MISSING} · {node.room or MISSING}"
        + _reading(node),
        columns=("EP", "TYPE", "CHANNEL", "NAME", "WHAT IS IN IT"),
        rows=tuple(
            Row(
                (
                    f"ep{e.number}",
                    e.type_name or "unknown",
                    str(e.channel),
                    e.name or MISSING,
                    _summary(e),
                ),
                NORMAL if e.type_name else DIM,
            )
            for e in node.detail
        ),
        items=tuple(node.detail),
        empty="the hub does not know a single endpoint yet",
    )


def _attributes(state: State, node: NodeView) -> Pane:
    selected = next((e for e in node.detail if e.number == state.endpoint), None)
    if selected is None:
        return Pane(note=("…",))
    features_text = f"features 0b{selected.features:b}" if selected.features else "features —"
    interesting = worth_showing(selected)
    return Pane(
        title=f"{node.alias or node.uid} · ep{selected.number} {selected.type_name} · "
        f"channel {selected.channel} · {features_text}" + _reading(node),
        columns=("ATTRIBUTE", "ON THE NODE", "IN THE FILE", "", "ACCESS", "KIND"),
        rows=tuple(
            Row(
                (
                    a.name,
                    _val(a.value, a.label),
                    _val(a.wanted, a.wanted_label),
                    _agreement(a),
                    a.access,
                    "configuration" if a.configurable else "state",
                ),
                BAD if a.matches is False else (GOOD if a.matches else NORMAL),
            )
            for a in interesting
        ),
        items=tuple(interesting),
        empty="the hub does not know a single value yet",
    )


def _reading(node: NodeView) -> str:
    """`G` runs in the background; the view polls rather than listens, so the title is
    where its progress shows (zadania/zrobione/FEAT-06.md, R10)."""
    return f" · reading, {node.reading} left" if node.reading is not None else ""


def _binding_detail(state: State) -> Pane:
    selected = state.selected()
    if not isinstance(selected, tuple):
        return Pane()
    binding, target = selected
    assert isinstance(binding, BindingView) and isinstance(target, TargetView)
    prose = [f"fired by: {'a report' if binding.on_report else 'an event'}"]
    if not target.active:
        prose.append("The target has no address today, so this binding DOES NOTHING.")
    return Pane(
        title=f"{binding.label or binding.source_alias} → {target.label or target.alias}",
        note=tuple(prose),
    )


def _diff_detail(state: State) -> Pane:
    selected = state.selected()
    if not isinstance(selected, DiffRow):
        return Pane()
    return Pane(
        title=f"{selected.alias or selected.uid} · ep{selected.endpoint} · {selected.attribute}",
        note=(
            f"in the file:  {_val(selected.wanted, selected.wanted_label)}",
            f"on the node:  {_val(selected.actual, selected.actual_label)}",
            "",
            "The hub writes the file to the node, never the other way round. Drift",
            "means reconciling has not got there yet, or did not succeed.",
        ),
    )


def _issue_detail(state: State) -> Pane:
    selected = state.selected()
    if not isinstance(selected, str):
        return Pane()
    return Pane(
        title="issue",
        note=(
            selected,
            "",
            "The hub does not guess at them — it shows them and leaves the decision "
            "(07-hub.md §5.3).",
        ),
    )


def _joining_detail(state: State) -> Pane:
    view = state.snapshot.joining
    status = "OPEN" if view and view.accepting else "closed"
    selected = state.selected()
    prose = [f"joining of new nodes: {status}"]
    if isinstance(selected, JoinView) and selected.state == "PENDING" and selected.address:
        prose += [
            "",
            f"This node works at 0x{selected.address:03X}, which the hub did not know —",
            "a lost or old database, or a module used elsewhere.",
            "It does not get in by itself, not even with joining open: decide.",
        ]
    elif isinstance(selected, JoinView) and selected.uid is None and not selected.address:
        prose += [
            "",
            "This node announces itself and gets no address.",
            f"Class {selected.node_class} — the hub does not know which block to take one from.",
        ]
    return Pane(
        title=selected.state if isinstance(selected, JoinView) else "joining",
        note=tuple(prose),
    )


def _bus_detail(state: State) -> Pane:
    """What differs, and the commands that will run — shown raw, so they can be pasted
    into a shell when something does not take (makiety, A8).

    All of it is prose, the table of changes included: the details pane draws either
    rows or prose, and here the commands under the table are the point. Nothing in it
    is selectable, so nothing is lost by not being rows."""
    bus = state.selected()
    if not isinstance(bus, BusSetupView):
        return Pane()
    where = " · ".join(part for part in (bus.hardware, bus.where) if part)
    title = f"{bus.name} · {bus.kind} · {bus.channel}" + (f" · {where}" if where else "")
    if bus.state == "error":
        return Pane(title=title, note=(bus.error, "", "The hub could not even look at it."))
    prose: list[str] = []
    if bus.fault:
        prose += [bus.fault, ""]
    if not bus.open:
        prose += ["The hub did not open this bus at start — see Issues; it takes a restart.", ""]
    if bus.changes:
        prose.append(f"{'PARAMETER':<24}{'NOW':<16}{'IN THE FILE':<16}")
        prose += [
            f"{c.what:<24}{c.current:<16}{c.desired:<16}{'BY HAND' if c.manual else ''}".rstrip()
            for c in bus.changes
        ]
        prose.append("")
    automatic = [change.how for change in bus.changes if not change.manual]
    if automatic:
        prose += ["[u] will run:", *(f"  {how}" for how in automatic)]
    for change in bus.changes:
        if change.manual:
            prose += ["", f"by hand — {change.what}:", f"  {change.how}"]
    if not bus.changes:
        prose.append("As the file says.")
    return Pane(title=title, note=tuple(prose))


def _health_detail(state: State) -> Pane:
    health = state.snapshot.health
    if health is None:
        return Pane(note=("no data",))
    lines = [
        f"nodes {health.ready}/{health.nodes} with an address, {health.configured} configured",
    ]
    if health.buses:
        width = max(len(bus.name) for bus in health.buses)
        lines += ["", "buses (the rate across the last few seconds):"]
        lines += [_bus_line(bus, width) for bus in health.buses]
    else:
        lines.append(f"buses: {MISSING}")
    lines.append("")
    if health.latency:
        lines.append(
            "fast path: p50 {p50:.0f} µs   p99 {p99:.0f} µs   max {max:.0f} µs   n {n:.0f}".format(
                **health.latency
            )
        )
    else:
        lines.append(f"fast path: {MISSING} (there has been no match yet)")
    lines.append("")
    for title, counters in (
        ("bindings", health.bindings),
        ("transactions", health.transactions),
        ("reconcile", health.reconcile),
        ("core", health.core),
    ):
        lines.append(f"{title}: " + "   ".join(f"{k} {v}" for k, v in counters.items()))
    return Pane(title="the hub's health", note=tuple(lines))


def _bus_line(bus: BusView, width: int) -> str:
    """`can0  rx 12.3/s  tx 3.0/s  ≈1.2 %  (rx 10234 · tx 2210 · dropped 0)`.

    The load carries `≈` because it is an estimate from above — worst-case bit stuffing
    (`core/busload.py`). Without a bitrate there is a dash, never a guessed percentage.
    """
    load = f"≈{bus.load * 100:.1f} %" if bus.load is not None else MISSING
    alarm = f"  {_LINK_ALARM[bus.link]}" if bus.link in _LINK_ALARM else ""
    return (
        f"{bus.name:<{width}}  rx {bus.rx_rate:.1f}/s  tx {bus.tx_rate:.1f}/s  {load}  "
        f"(rx {bus.rx} · tx {bus.tx} · dropped {bus.dropped}){alarm}"
    )


_DETAILS: Final[dict[Section, Callable[[State], Pane]]] = {
    Section.NODES: _node_detail,
    Section.BINDINGS: _binding_detail,
    Section.DIFF: _diff_detail,
    Section.ISSUES: _issue_detail,
    Section.BUSES: _bus_detail,
    Section.JOINING: _joining_detail,
    Section.HEALTH: _health_detail,
}


def worth_showing(endpoint: EndpointView) -> list[AttributeView]:
    """Which attributes we show at all.

    A known value, a value from the file, or something that **can be set**. The third is
    here so that the pane says not only "what is" but also "what may be" — otherwise an
    unset configuration attribute would be invisible until the first write.
    """
    return [
        a
        for a in endpoint.attributes
        if a.value is not None or a.wanted is not None or a.configurable
    ]


# --- keys that depend on what is selected -----------------------------------------


def context_keys(state: State) -> tuple[tuple[str, str], ...]:
    """Keys that depend on what is selected — **computed separately from the rows**.

    Separately, because they ask about the selection and the selection asks about the
    rows: computing them together would recurse. Along the way, what is visible on that
    line is exactly what will work — nothing has to be remembered.
    """
    if state.section is Section.NODES:
        return _node_actions(state)
    if state.section is Section.DIFF and isinstance(state.selected(), DiffRow):
        return (("R", "reconcile now"), ("p", "adopt the value from the node into the file"))
    if state.section is Section.BUSES:
        selected = state.selected()
        if isinstance(selected, BusSetupView) and any(not c.manual for c in selected.changes):
            return (("u", "reconcile — the bus goes down for a moment"),)
        return ()
    if state.section is Section.BINDINGS:
        out: list[tuple[str, str]] = [("+", "add a binding")]
        if state.selected() is not None:
            out.append(("-", "remove the binding"))
        return tuple(out)
    if state.section is Section.JOINING:
        selected = state.selected()
        if isinstance(selected, JoinView) and selected.state == "PENDING" and selected.address:
            return (
                ("y", "admit at its address"),
                ("X", "reset to factory settings"),
                ("B", "block"),
            )
        if isinstance(selected, JoinView) and selected.state == "BLOCKED" and selected.uid:
            return (("B", "unblock"), ("a", "open joining for 15 min"))
        return (("a", "open joining for 15 min"),)
    return ()


def _node_actions(state: State) -> tuple[tuple[str, str], ...]:
    if state.focus is Focus.DETAIL and state.endpoint is not None:
        return _attribute_actions(state)
    out: list[tuple[str, str]] = []
    if state.output_endpoint() is not None:
        out += [("t", "Toggle"), ("o", "On"), ("f", "Off")]
    endpoint = state.detail_selected() if state.focus is Focus.DETAIL else None
    if isinstance(endpoint, EndpointView) and any(c.available for c in endpoint.commands):
        out.append(("c", "send a command"))
    if state.node_ref():
        out += [("i", "Identify"), ("R", "reconcile"), ("n", "name"), ("N", "alias")]
        selected = state.selected()
        blocked = isinstance(selected, NodeSummary) and selected.blocked
        out += [("E", "enumerate"), ("B", "unblock" if blocked else "block"), ("X", "remove")]
        out.append(("G", "read everything"))
    return tuple(out)


def _attribute_actions(state: State) -> tuple[tuple[str, str], ...]:
    selected = state.detail_selected()
    if not isinstance(selected, AttributeView):
        return ()
    out: list[tuple[str, str]] = []
    if selected.configurable:
        out.append(("e", "edit in the file"))
        if selected.wanted is not None:
            out.append(("u", "remove from the file"))
        if selected.value is not None and selected.matches is False:
            out.append(("p", "adopt from the node"))
    if "W" in selected.access:
        out.append(("w", "set on the node"))
    if "R" in selected.access:
        out.append(("g", "read"))
    out.append(("G", "read the endpoint"))
    out.append(("c", "send a command"))
    return tuple(out)


# --- the traffic feed --------------------------------------------------------------


def traffic_row(item: TrafficView) -> tuple[str, int]:
    """One feed row and its colour."""
    arrow = "→" if item.outgoing else " "
    kind = KIND_LABELS.get(item.kind, item.kind)
    where = f"0x{item.address:03X} ep{item.endpoint}" if item.kind != "JOIN" else ""
    what = item.name or (f"0x{item.element:02X}" if item.kind != "JOIN" else "")
    if item.detail:
        what = f"{what}={item.detail}" if what else item.detail
    tail = ""
    if item.tid is not None:
        tail = f"tid={item.tid}"
    if item.result:
        what = f"{what} {item.result}".strip()
    took = f"{item.elapsed * 1000:.1f} ms" if item.elapsed is not None else ""
    if item.late:
        took = f"{took} late".strip()
    if item.retry:
        took = f"{took} retry {item.retry}".strip()
    colour = NORMAL
    if item.result and item.result != "OK":
        colour = BAD
    elif item.kind in ("REPORT", "READ"):
        colour = DIM
    elif item.kind == "ALARM":
        colour = BAD
    return (
        f"{item.at:8.3f} {arrow} {item.bus:<6} {kind:<11} {where:<11} {what:<22} {tail:<7} {took}"
    ).rstrip(), colour


# --- small change -----------------------------------------------------------------


def _summary(e: EndpointView) -> str:
    """The endpoint’s most important value, in one piece."""
    for name in ("OnOff", "Value", "Purpose", "EventMode"):
        meta = next((a for a in e.attributes if a.name == name), None)
        if meta is not None and meta.value is not None:
            return f"{name} = {_val(meta.value, meta.label)}"
    return MISSING


def _agreement(a: AttributeView) -> str:
    if a.matches is None:
        return ""
    return "✓" if a.matches else "≠"


def _val(value: Plain, label: str | None) -> str:
    if value is None:
        return MISSING
    if label:
        return label
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _addr(address: int) -> str:
    return f"0x{address:03X}" if address else MISSING


def _age(seconds: float | None) -> str:
    if seconds is None:
        return MISSING
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


__all__ = [
    "BAD",
    "DIM",
    "GOOD",
    "HEADER",
    "MISSING",
    "NORMAL",
    "SELECTED",
    "Pane",
    "Row",
    "details",
    "items",
    "traffic_row",
]
