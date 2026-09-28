"""Data -> text. Separate from the commands’ content, because text is one output of
several.

The full-screen view reads the same `Result`s and draws them its own way; had
`commands.py` returned finished strings, it would have to take them apart again
([`ARCHITEKTURA.md`](../../../ARCHITEKTURA.md) §10.3).

The rule for this whole layer: **"I do not know" does not look like "zero"**. The hub
does not poll nodes after they join and restores state from reports
(01-model-danych.md §6), so until a report arrives the honest answer is a dash, not
`false`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Final

from lagunhub.adapters.cli.commands import Result
from lagunhub.api import (
    ActionView,
    AttributeView,
    BindingView,
    CommissioningView,
    ConfigView,
    DiffRow,
    EndpointView,
    EventView,
    HealthView,
    HubInfo,
    NodeSummary,
    NodeView,
)

MISSING: Final = "—"
"""What the hub does not know. One character for the whole CLI, so the eye can hunt for
it."""

VERDICTS: Final[dict[str, str]] = {
    "match": "agrees",
    "mismatch": "DRIFT",
    "unknown": "unknown",
    "missing": "NO EP",
}


def render(result: Result) -> str:
    draw = _RENDERERS.get(result.kind)
    if draw is None:
        return result.message
    content = draw(result.data)
    if result.message and content:
        return f"{result.message}\n{content}"
    return result.message or content


# --- the individual views --------------------------------------------------------


def _nodes(nodes: Sequence[NodeSummary]) -> str:
    if not nodes:
        return "the installation is empty"
    return _table(
        ("ALIAS", "NAME", "UID", "ADDR", "STATE", "EP", "HEARD"),
        [
            (
                node.alias or MISSING,
                node.name or MISSING,
                node.uid,
                f"0x{node.address:03X}" if node.address else MISSING,
                node.state,
                str(node.endpoints) if node.endpoints else MISSING,
                _age(node.silent_for),
            )
            for node in nodes
        ],
    )


def _node(node: NodeView) -> str:
    header = [
        f"{node.alias or node.uid}  {node.name}".strip(),
        f"  uid {node.uid}   address "
        + (f"0x{node.address:03X}" if node.address else MISSING)
        + f"   bus {node.bus or MISSING}   state {node.state}",
        f"  device 0x{node.device_type:04X}   vendor 0x{node.vendor_id:04X}   "
        f"firmware {_firmware(node.firmware)}",
    ]
    if node.room:
        header.append(f"  room {node.room}")
    parts = ["\n".join(header)]
    for endpoint in node.detail:
        parts.append(_endpoint(endpoint))
    return "\n\n".join(parts)


def _endpoint(endpoint: EndpointView) -> str:
    title = f"  ep{endpoint.number}  {endpoint.type_name or 'unknown'}"
    if endpoint.name:
        title += f"  „{endpoint.name}”"
    details = [f"channel {endpoint.channel}"]
    if endpoint.features:
        details.append(f"features 0b{endpoint.features:b}")
    if endpoint.quantity:
        details.append(endpoint.quantity)
    if endpoint.groups:
        details.append("groups " + ",".join(str(g) for g in endpoint.groups))
    title += "  (" + ", ".join(details) + ")"

    # A known value, a value from the file, or something that CAN be set. The third is
    # here so that `ep` says not only "what is" but also "what may be" — otherwise an
    # unset configuration attribute would be invisible until the first write.
    interesting = [
        a
        for a in endpoint.attributes
        if a.value is not None or a.wanted is not None or a.configurable
    ]
    if not interesting:
        return title + "\n    (the hub does not know a single value yet)"
    return (
        title
        + "\n"
        + _indent(
            _table(
                ("ATTRIBUTE", "VALUE", "IN THE FILE", "", "ACCESS"),
                [
                    (
                        a.name,
                        _value(a.value, a.label),
                        _value(a.wanted, a.wanted_label),
                        _match(a),
                        a.access,
                    )
                    for a in interesting
                ],
            ),
            4,
        )
    )


def _action(action: ActionView) -> str:
    tail = f"  ({action.attempts} attempts, {action.elapsed * 1000:.1f} ms)"
    if action.data:
        return f"{action.result}  {action.data}{tail}"
    return f"{action.result}{tail}"


def _diff(rows: Sequence[DiffRow]) -> str:
    if not rows:
        return "there is nothing to reconcile"
    drifts = [r for r in rows if r.state == "mismatch"]
    strangers = [r for r in rows if r.state in ("unknown", "missing")]
    table = _table(
        ("NODE", "EP", "ATTRIBUTE", "IN THE FILE", "ON THE NODE", "STATE"),
        [
            (
                r.alias or r.uid,
                f"ep{r.endpoint}",
                r.attribute,
                _value(r.wanted, r.wanted_label),
                _value(r.actual, r.actual_label),
                VERDICTS.get(r.state, r.state),
            )
            for r in rows
        ],
    )
    # "I do not know" is not the same as "does not agree" and the two must not be
    # merged: the first means the hub has not read it yet, the second that the node has
    # something else.
    summary = []
    if drifts:
        summary.append(f"{len(drifts)} disagree with the file")
    if strangers:
        summary.append(f"{len(strangers)} not read yet")
    if not summary:
        return table + f"\n\nall {len(rows)} agree"
    return table + "\n\n" + ", ".join(summary) + f" (of {len(rows)})"


def _issues(issues: Sequence[str]) -> str:
    if not issues:
        return "nothing needs attention"
    return "\n".join(f"  • {issue}" for issue in issues)


def _health(health: HealthView) -> str:
    lines = [
        f"nodes {health.ready}/{health.nodes} with an address, {health.configured} configured, "
        f"issues {health.issues}",
        "buses: " + (", ".join(bus.name for bus in health.buses) or MISSING),
    ]
    if health.latency:
        lines.append(
            "bindings: p50 {p50:.0f} µs, p99 {p99:.0f} µs, max {max:.0f} µs "
            "({n:.0f} samples)".format(**health.latency)
        )
    for title, counters in (
        ("bindings", health.bindings),
        ("core", health.core),
        ("transactions", health.transactions),
        ("reconcile", health.reconcile),
    ):
        lines.append(f"{title}: " + ", ".join(f"{k} {v}" for k, v in counters.items()))
    return "\n".join(lines)


def _bindings(bindings: Sequence[BindingView]) -> str:
    if not bindings:
        return "there are no bindings"
    lines = []
    for binding in bindings:
        source = binding.label or f"{binding.source_alias} ep{binding.source_endpoint}"
        for target in binding.targets:
            marker = " " if target.active else "!"
            label = target.label or f"{target.alias} ep{target.endpoint}"
            lines.append(f" {marker} {source}  →  {label}")
    dead = sum(1 for b in bindings for t in b.targets if not t.active)
    if dead:
        lines.append(
            f"\n! {dead} without a target — the target has no address, so the binding does nothing"
        )
    return "\n".join(lines)


def _commissioning(view: CommissioningView) -> str:
    state = "OPEN" if view.accepting else "closed"
    if not view.known:
        return f"joining {state}; nobody has announced yet"
    return f"joining {state}\n" + _table(
        ("SHORTID", "CLASS", "ADDRESS", "STATE", "UID"),
        [
            (
                f"0x{record.short_id:08X}",
                record.node_class,
                f"0x{record.address:03X}" if record.address else MISSING,
                record.state,
                record.uid or MISSING,
            )
            for record in view.known
        ],
    )


def _config(view: ConfigView) -> str:
    files = ", ".join(view.files) or MISSING
    return f"{files}   version {view.etag}"


def _info(info: HubInfo) -> str:
    return (
        f"{info.name} {info.version} — nodes {info.ready}/{info.nodes}, "
        f"issues {info.issues}, buses {', '.join(info.buses) or MISSING}"
    )


def event(item: EventView) -> str:
    """One line of the feed. Narrow, because it runs in the background under the
    prompt."""
    where = f"{item.uid[:8]}" if item.uid else "hub"
    if item.kind == "state":
        return f"  {where} ep{item.endpoint} {item.name} = {_value(item.value, None)}"
    if item.kind == "event":
        return f"  {where} ep{item.endpoint} {item.name}"
    if item.kind == "issue":
        return f"  ! {item.text}"
    return f"  {where} {item.name} {item.text}".rstrip()


_RENDERERS: Final[dict[str, Any]] = {
    "nodes": _nodes,
    "node": _node,
    "endpoint": _endpoint,
    "action": _action,
    "diff": _diff,
    "issues": _issues,
    "health": _health,
    "bindings": _bindings,
    "commissioning": _commissioning,
    "config": _config,
    "info": _info,
}


# --- small change ----------------------------------------------------------------


def _table(columns: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    all_rows = [tuple(row) for row in rows]
    if not all_rows:
        return ""
    widths = [max(len(columns[i]), *(len(row[i]) for row in all_rows)) for i in range(len(columns))]
    out = [_row(columns, widths)]
    out.extend(_row(row, widths) for row in all_rows)
    return "\n".join(out)


def _row(cells: Sequence[str], widths: Sequence[int]) -> str:
    return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()


def _indent(text: str, how_many: int) -> str:
    indent = " " * how_many
    return "\n".join(indent + line if line else line for line in text.split("\n"))


def _value(value: Any, label: str | None) -> str:
    """`None` is **"I do not know"**, not "zero" — and it is to look that way."""
    if value is None:
        return MISSING
    if label:
        return label
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _match(a: AttributeView) -> str:
    if a.matches is None:
        return ""
    return "✓" if a.matches else "≠"


def _age(seconds: float | None) -> str:
    if seconds is None:
        return MISSING
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _firmware(version: int) -> str:
    if not version:
        return MISSING
    return f"{version >> 24}.{(version >> 16) & 0xFF}.{version & 0xFFFF}"


__all__ = ["MISSING", "event", "render"]
