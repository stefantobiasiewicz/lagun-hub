"""Actions that need more than one question.

Sending a command with arguments, naming a node and assembling a binding share one
trait: **they need data the view does not have yet**. The target’s endpoints have to be
fetched, and so does the command list. That is why every step goes through the worker
thread, and the next step’s window is set up only once the answer has arrived — never on
the drawing thread ([`state.py`](state.py)).

A binding refers to nodes by **alias**, not by `Uid`, because that is what the file
looks like ([`KONFIGURACJA.md`](../../../KONFIGURACJA.md) §5). A node without an alias
therefore cannot be bound — and the view says so outright instead of writing something
the loader will not read.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from lagunhub.adapters.tui import overlay
from lagunhub.adapters.tui.state import State
from lagunhub.api import AttributeView, CommandView, EndpointView, NodeSummary, NodeView

Next = Callable[[Any], None]


# --- a command to an endpoint -----------------------------------------------------


def send_command(state: State, ref: str, endpoint: EndpointView) -> None:
    """A choice of command, and then — if needed — of its arguments."""
    available_ones = tuple(c for c in endpoint.commands if c.available)
    if not available_ones:
        state.say(f"! ep{endpoint.number} has not a single command that can be sent")
        return

    def chosen(name: Any) -> None:
        meta = next(c for c in available_ones if c.name == str(name))
        if not meta.args:
            _send(state, ref, endpoint.number, meta.name, {})
            return
        state.overlay = overlay.Form(
            title=f"{meta.name} — arguments",
            subject=f"{ref} · ep{endpoint.number} · {meta.name}",
            fields=tuple(
                overlay.Field(a.name, _argument_hint(a), _now(endpoint, a.name) or _default(a))
                for a in meta.args
            ),
            on_accept=lambda values: _send(
                state, ref, endpoint.number, meta.name, _arguments(values)
            ),
        )

    state.overlay = overlay.Choose(
        title="Command to the node",
        subject=f"{ref} · ep{endpoint.number} {endpoint.type_name}",
        options=tuple(c.name for c in available_ones),
        on_accept=chosen,
        note=_left_out(endpoint.commands),
    )


def _left_out(all_of: tuple[CommandView, ...]) -> str:
    missing = [c.name for c in all_of if not c.available]
    if not missing:
        return ""
    return f"no feature, so unavailable: {', '.join(missing)}"


def _argument_hint(arg: Any) -> str:
    span = f" {arg.lo}…{arg.hi}" if arg.lo is not None else ""
    return f"{arg.type}{span}"


NOW: Final = {"level": "Level", "r": "Red", "g": "Green", "b": "Blue", "w": "White"}
"""An argument that sets an attribute starts from that attribute's value: `SetColor` opens
at the colour the light has, so changing the blue is one field (DEVICE-03)."""


def _now(endpoint: EndpointView, arg: str) -> str:
    name = NOW.get(arg)
    value = next((a.value for a in endpoint.attributes if a.name == name), None)
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else ""


def _default(arg: Any) -> str:
    return "0" if arg.lo is None or arg.lo <= 0 <= (arg.hi or 0) else str(arg.lo)


def _arguments(values: dict[str, str]) -> dict[str, Any]:
    return {name: _number(text.strip()) for name, text in values.items()}


def _send(state: State, ref: str, endpoint: int, name: str, args: dict[str, Any]) -> None:
    def task() -> str:
        result = state.hub.command(ref, endpoint, name, args)
        state.reload()
        return f"{ref} ep{endpoint} {name}: {result.result}"

    state.worker.submit(task)


# --- naming a node ----------------------------------------------------------------


def name_node(state: State, ref: str, uid: str, field: str) -> None:
    """`name` is a label for a person, `alias` a handle for bindings.

    Both go into the file, because both are human decisions: the hub will not recover
    them from the bus
    ([`KONFIGURACJA.md`](../../../KONFIGURACJA.md) §1).
    """
    live = ""
    node = state.snapshot.node
    if isinstance(node, NodeView) and node.uid == uid:
        live = node.alias if field == "alias" else node.name

    def save(value: Any) -> None:
        text = str(value).strip()
        if not text:
            state.say("! an empty name means nothing — use [u] to remove it")
            return

        def task() -> str:
            state.hub.config_set(f"nodes/{uid}/{field}", text, etag=state.hub.config().etag)
            state.reload()
            return f"{field} = {text} — saved in the file"

        state.worker.submit(task)

    state.overlay = overlay.Edit(
        title=f"node {field}",
        subject=f"{ref}\nuid {uid}",
        hint=(
            "an alias is a handle for bindings — no spaces"
            if field == "alias"
            else "a name is a label for a person — anything goes"
        ),
        buffer=live,
        on_accept=save,
        note="The write goes to the file, because this is a human decision, not a node’s.",
    )


# --- the binding wizard -----------------------------------------------------------


REPORT: Final = "report: "
"""How a reported attribute is told apart from an event in one list of sources."""


def add_binding(state: State) -> None:
    """Six questions: from where, what, and to where. Each from a list — there is nothing
    to type.

    Two shapes: an event fires a command without arguments, a reported attribute fills a
    writable attribute of the same type with its value (07-hub.md §3.2;
    zadania/zrobione/FEAT-13.md). The lists offer only what the loader will accept."""
    sources = _with_an_alias(state)
    if not sources:
        state.say("! no node has an alias — give one with [N] in the Nodes section")
        return
    _choose_node(state, "Binding · source", sources, lambda alias: _source_endpoint(state, alias))


def _source_endpoint(state: State, alias: str) -> None:
    def next_step(node: NodeView) -> None:
        endpoints = tuple(e for e in node.detail if e.events or _reported(e))
        if not endpoints:
            state.say(f"! {alias} has no endpoint that reports anything")
            return
        _choose_endpoint(
            state,
            f"Binding · {alias} · source endpoint",
            endpoints,
            lambda e: _source_event(state, alias, e),
        )

    _fetch(state, alias, next_step)


def _reported(endpoint: EndpointView) -> tuple[AttributeView, ...]:
    return tuple(a for a in endpoint.attributes if a.reported and a.available)


def _source_event(state: State, alias: str, endpoint: EndpointView) -> None:
    attributes = {f"{REPORT}{a.name}": a for a in _reported(endpoint)}

    def chosen(selected: Any) -> None:
        if str(selected) in attributes:
            _write_node(state, alias, endpoint, attributes[str(selected)])
        else:
            _target_node(state, alias, endpoint, str(selected))

    state.overlay = overlay.Choose(
        title="Binding · source",
        subject=f"{alias} · ep{endpoint.number} {endpoint.type_name}",
        options=(*endpoint.events, *attributes),
        on_accept=chosen,
        note="an event fires a command; a report writes its value into an attribute",
    )


# --- report → write -----------------------------------------------------------------


def _write_node(state: State, alias: str, source: EndpointView, attribute: AttributeView) -> None:
    _choose_node(
        state,
        "Binding · target",
        _with_an_alias(state),
        lambda target_alias: _write_endpoint(state, alias, source, attribute, str(target_alias)),
    )


def _writable(endpoint: EndpointView, like: AttributeView) -> tuple[str, ...]:
    """Same type only: the hub copies the value and converts nothing — `<Q>` against
    `<Q>` included, whose quantities nobody checks yet (zadania/zrobione/FEAT-13.md N4)."""
    return tuple(
        a.name
        for a in endpoint.attributes
        if "W" in a.access and a.available and a.type == like.type and a.type != "string"
    )


def _write_endpoint(
    state: State, alias: str, source: EndpointView, attribute: AttributeView, target_alias: str
) -> None:
    def next_step(node: NodeView) -> None:
        endpoints = tuple(e for e in node.detail if _writable(e, attribute))
        if not endpoints:
            state.say(f"! {target_alias} has no writable {attribute.type} attribute")
            return
        _choose_endpoint(
            state,
            f"Binding · {target_alias} · target endpoint",
            endpoints,
            lambda e: _write_attribute(state, alias, source, attribute, target_alias, e),
        )

    _fetch(state, target_alias, next_step)


def _write_attribute(
    state: State,
    alias: str,
    source: EndpointView,
    attribute: AttributeView,
    target_alias: str,
    target: EndpointView,
) -> None:
    state.overlay = overlay.Choose(
        title="Binding · attribute",
        subject=f"{alias} ep{source.number} {attribute.name}  →  {target_alias} ep{target.number}",
        options=_writable(target, attribute),
        on_accept=lambda name: _save(
            state,
            {
                "node": alias,
                "endpoint": source.number,
                "type": source.type_name,
                "attribute": attribute.name,
            },
            {
                "node": target_alias,
                "endpoint": target.number,
                "type": target.type_name,
                "attribute": str(name),
            },
            f"binding {alias} {attribute.name} -> {target_alias} {name} saved",
        ),
        note="every report of the source writes its value here, as it is",
    )


# --- event → command ----------------------------------------------------------------


def _target_node(state: State, alias: str, endpoint: EndpointView, event: str) -> None:
    _choose_node(
        state,
        "Binding · target",
        _with_an_alias(state),
        lambda target_alias: _target_endpoint(state, alias, endpoint, event, str(target_alias)),
    )


def _target_endpoint(
    state: State, alias: str, endpoint: EndpointView, event: str, target_alias: str
) -> None:
    def next_step(node: NodeView) -> None:
        endpoints = tuple(e for e in node.detail if any(c.available for c in e.commands))
        if not endpoints:
            state.say(f"! {target_alias} has no endpoint that takes commands")
            return
        _choose_endpoint(
            state,
            f"Binding · {target_alias} · target endpoint",
            endpoints,
            lambda e: _target_command(state, alias, endpoint, event, target_alias, e),
        )

    _fetch(state, target_alias, next_step)


def _target_command(
    state: State,
    alias: str,
    source: EndpointView,
    event: str,
    target_alias: str,
    target: EndpointView,
) -> None:
    available_ones = tuple(c.name for c in target.commands if c.available and not c.args)
    if not available_ones:
        state.say("! this endpoint only has commands with arguments — we do not bind those yet")
        return
    state.overlay = overlay.Choose(
        title="Binding · command",
        subject=f"{alias} ep{source.number} {event}  →  {target_alias} ep{target.number}",
        options=available_ones,
        on_accept=lambda command: _save_binding(
            state, alias, source, event, target_alias, target, str(command)
        ),
        note="commands with arguments are added in the file — there would be too much to ask "
        "for here",
    )


def _save_binding(
    state: State,
    alias: str,
    source: EndpointView,
    event: str,
    target_alias: str,
    target: EndpointView,
    command: str,
) -> None:
    _save(
        state,
        {"node": alias, "endpoint": source.number, "type": source.type_name, "event": event},
        {
            "node": target_alias,
            "endpoint": target.number,
            "type": target.type_name,
            "command": command,
        },
        f"binding {alias} {event} -> {target_alias} {command} saved",
    )


def _save(state: State, source: dict[str, Any], target: dict[str, Any], done: str) -> None:
    entry = {"from": source, "to": target}

    def task() -> str:
        view = state.hub.config()
        live = list((view.document or {}).get("bindings") or [])
        live.append(entry)
        state.hub.config_set("bindings", live, etag=view.etag)
        state.reload()
        return done

    state.worker.submit(task)


def remove_binding(state: State, alias: str, endpoint: int) -> None:
    """Removes the **whole entry** for this source — because in the file one entry has one
    source."""

    def carry_out(_: Any) -> None:
        def task() -> str:
            view = state.hub.config()
            live = list((view.document or {}).get("bindings") or [])
            remain = [
                w
                for w in live
                if not (
                    str((w.get("from") or {}).get("node")) == alias
                    and int((w.get("from") or {}).get("endpoint", -1)) == endpoint
                )
            ]
            removed = len(live) - len(remain)
            if not removed:
                return "! I did not find that entry in the file"
            state.hub.config_set("bindings", remain, etag=view.etag)
            state.reload()
            return f"entries removed: {removed}"

        state.worker.submit(task)

    state.overlay = overlay.Confirm(
        title="Remove the binding?",
        lines_before=(
            "Every entry whose source is this will disappear:",
            f"  {alias} ep{endpoint}",
            "",
            "The file is written at once; the nodes lose nothing.",
        ),
        accept="remove",
        on_accept=carry_out,
    )


# --- shared -----------------------------------------------------------------------


def _with_an_alias(state: State) -> tuple[NodeSummary, ...]:
    return tuple(n for n in state.snapshot.nodes if n.alias)


def _choose_node(state: State, title: str, nodes: tuple[NodeSummary, ...], next_step: Next) -> None:
    state.overlay = overlay.Choose(
        title=title,
        subject="choose a node",
        options=tuple(n.alias for n in nodes),
        on_accept=next_step,
        note="bindings refer to the alias, not to the Uid — that is what the file looks like",
    )


def _choose_endpoint(
    state: State, title: str, endpoints: tuple[EndpointView, ...], next_step: Next
) -> None:
    labels = {f"ep{e.number} {e.type_name}": e for e in endpoints}
    state.overlay = overlay.Choose(
        title=title,
        subject="choose an endpoint",
        options=tuple(labels),
        on_accept=lambda selected: next_step(labels[str(selected)]),
    )


def _fetch(state: State, ref: str, next_step: Callable[[NodeView], None]) -> None:
    """The endpoints have to be fetched — and that is a conversation with the hub, so it
    goes through the thread."""

    def task() -> str:
        node = state.hub.node(ref)
        next_step(node)
        state.dirty = True
        return ""

    state.worker.submit(task)


def _number(text: str) -> Any:
    try:
        return int(text, 0)
    except ValueError:
        return text


__all__ = ["add_binding", "name_node", "remove_binding", "send_command"]
