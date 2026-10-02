"""The full-screen view — the loop, the keys, the windows on top.

**Library: `curses` from the standard library.** We considered `Textual` and `urwid`;
both give a prettier result with less code, but both drag in an `asyncio` loop of their
own that would have to be bridged to a client blocking on a socket — the same argument
that settled **H7** on the server. The question of an optional dependency disappears
along the way: there is none at all.

This file **knows nothing about the hub** beyond `State`, and `State` talks to it through
the hub client ([`adapters/client/`](../client/client.py)). [`panes.py`](panes.py) makes
the panes’ content and [`screen.py`](screen.py) draws it — so one can be checked without a
terminal and the other with a mock screen.
"""

from __future__ import annotations

import curses
import locale
from collections.abc import Callable
from typing import Any, Final

from lagunhub.adapters.client.client import Hub
from lagunhub.adapters.tui import actions, overlay, screen
from lagunhub.adapters.tui.state import Section, State
from lagunhub.api import ApiError, AttributeView, DiffRow, EndpointView, JoinView, NodeSummary

TICK_MS: Final = 100
"""How long to wait for a key before redrawing. Ten frames per second is enough for the
eye and **does not depend on how much traffic there is** — the view redraws at a fixed
rate rather than after every frame
([`ARCHITEKTURA.md`](../../../ARCHITEKTURA.md) §10.3.3)."""

HELP: Final[tuple[str, ...]] = (
    "↑ ↓ PgUp PgDn   move through the list — through EVERY list, sections too",
    "→  ⏎            deeper: sections -> list -> endpoints -> attributes",
    "←  ⌫  Esc       back; at the very top — quit",
    "⇥  ⇧⇥           next / previous section",
    "/               filter the list   r   refresh now",
    "space           pause the feed    q   quit",
    "",
    "on a node:     t Toggle   o On   f Off   i Identify   R reconcile",
    "               n name     N alias        c send a command",
    "               E enumerate again   B block / unblock   X remove from the hub",
    "on an attribute: e edit in the file   w set on the node",
    "               u remove from the file  g read from the node",
    "               p adopt the value from the node INTO THE FILE",
    "bindings:      + add     - remove",
    "joining:       a open    y admit a PENDING node   X reset it to factory",
    "               B block a PENDING node — the hub then ignores it",
    "",
    "e goes to the FILE, and reaches the node by reconciling.",
    "w goes STRAIGHT to the node and does not touch the file.",
)


def run(hub: Hub) -> int:
    locale.setlocale(locale.LC_ALL, "")
    state = State(hub)
    state.reload()
    state.start()
    try:
        curses.wrapper(lambda stdscr: _loop(stdscr, state))
    finally:
        state.stop()
    return 0


def _loop(stdscr: Any, state: State) -> None:
    curses.curs_set(0)
    stdscr.timeout(TICK_MS)
    screen.init_colors()
    while True:
        if state.dirty:
            state.dirty = False
            screen.draw(stdscr, state)
        key_sim = stdscr.getch()
        if key_sim == -1:
            state.dirty = True  # the header clock and the feed keep going
            continue
        if key_sim == curses.KEY_RESIZE:
            state.dirty = True
            continue
        if not key(state, key_sim):
            return


# --- keys -------------------------------------------------------------------------


def key(state: State, key_sim: int) -> bool:
    """`False` means the end. Split out of the loop so it can be checked by test."""
    state.dirty = True
    if state.overlay is not None:
        # The window closes on confirmation — but `on_accept` may have opened the NEXT
        # one meanwhile, and then closing would clear the wrong thing. So we close only
        # the window that received the key. Without this no chain of questions works:
        # neither a command with arguments nor the binding wizard.
        live = state.overlay
        if not live.key(key_sim) and state.overlay is live:
            state.overlay = None
        return True
    state.status = ""

    if key_sim in (ord("q"), ord("Q")):
        return False
    action = _KEYS.get(key_sim)
    if action is not None:
        return action(state)
    letter = _LETTERS.get(key_sim)
    if letter is not None:
        letter(state)
    return True


def _back(state: State) -> bool:
    """Back **never ends the program**.

    Quitting is on `q` and only there. A key one walks with must not do something
    irreversible on the tenth press — and at the top of a list a person presses `←` one
    time too many without thinking.
    """
    if state.filter:
        state.filter = ""
    else:
        state.back()
    return True


def _move(delta: int) -> Callable[[State], bool]:
    def action(state: State) -> bool:
        state.move(delta)
        return True

    return action


def _section(delta: int) -> Callable[[State], bool]:
    def action(state: State) -> bool:
        state.section_by(delta)
        return True

    return action


def _enter(state: State) -> bool:
    state.enter()
    return True


_KEYS: Final[dict[int, Callable[[State], bool]]] = {
    curses.KEY_UP: _move(-1),
    curses.KEY_DOWN: _move(1),
    curses.KEY_PPAGE: _move(-10),
    curses.KEY_NPAGE: _move(10),
    curses.KEY_HOME: _move(-10_000),
    curses.KEY_END: _move(10_000),
    ord("\t"): _section(1),
    curses.KEY_BTAB: _section(-1),
    # The side arrows go DEEPER, not between sections: -> enters, <- returns. Sections
    # stay on tab, because that is the only move that jumps context.
    curses.KEY_RIGHT: _enter,
    curses.KEY_LEFT: _back,
    curses.KEY_ENTER: _enter,
    10: _enter,
    13: _enter,
    curses.KEY_BACKSPACE: _back,
    127: _back,
    8: _back,
    27: _back,
}


def _toggle(state: State, command: str) -> None:
    ref = state.node_ref()
    endpoint = state.output_endpoint()
    if ref is None or endpoint is None:
        state.say("! nothing here that can be driven")
        return
    state.worker.submit(lambda: _command(state, ref, endpoint.number, command))


def _command(state: State, ref: str, endpoint: int, command: str) -> str:
    result = state.hub.command(ref, endpoint, command)
    state.reload()
    return f"{ref} ep{endpoint} {command}: {result.result}"


def _identify(state: State) -> None:
    ref = state.node_ref()
    if ref is None:
        return
    state.worker.submit(lambda: f"{ref}: Identify {state.hub.identify(ref, 5).result}")


def _accept(state: State) -> None:
    def task() -> str:
        view = state.hub.accept_new(900.0)
        state.reload()
        return "joining open for 15 min" if view.accepting else "joining closed"

    state.worker.submit(task)


def _stranger(state: State) -> JoinView | None:
    """The joining row under the cursor, if it is a node found at an address the hub did
    not know (06-cykl-zycia-urzadzenia.md §2.3) — the only kind these two keys act on."""
    selected = state.selected()
    if (
        state.section is not Section.JOINING
        or not isinstance(selected, JoinView)
        or selected.state != "PENDING"
        or not selected.address
    ):
        return None
    return selected


def _admit(state: State) -> None:
    row = _stranger(state)
    if row is None:
        state.say("! choose a PENDING node that has an address")
        return

    def task() -> str:
        state.hub.admit(row.short_id)
        state.reload()
        return f"0x{row.short_id:08X} admitted at 0x{row.address:03X} — enumerating"

    state.worker.submit(task)


def _factory_reset(state: State) -> None:
    """Irreversible for the node — so confirmed by retyping, like removing a binding."""
    row = _stranger(state)
    if row is None:
        state.say("! choose a PENDING node that has an address")
        return
    name = f"{row.short_id:08X}"

    def carry_out(_: str) -> None:
        def task() -> str:
            result = state.hub.factory_reset(row.short_id)
            state.reload()
            return f"0x{name}: FactoryReset {result.result} — it goes back to joining"

        state.worker.submit(task)

    state.overlay = overlay.Confirm(
        title="Reset to factory settings?",
        lines_before=(
            f"The node at 0x{row.address:03X} forgets its address and everything a person",
            "set on it, then announces itself like a new device.",
        ),
        expect=name,
        prompt="type the ShortId to confirm:",
        on_accept=carry_out,
    )


def _x(state: State) -> None:
    """`X` is the irreversible key in two places: a stranger is reset, a node is removed."""
    if state.section is Section.NODES:
        _remove_node(state)
    else:
        _factory_reset(state)


REMOVE_WAIT: Final = "remove — the node confirms its reset first"
REMOVE_FORCE: Final = "force — send the reset, do not wait, forget it anyway"


def _remove_node(state: State) -> None:
    """Forget a node: the hub, the file and its bindings (06-cykl-zycia-urzadzenia.md §6).

    One window: choosing the mode **is** the confirmation, `Esc` backs out. No retyping
    of the alias, unlike the factory reset of a stranger — the user found it pointless
    here (zadania/zrobione/FEAT-04.md): the node is named in the window, and an ordinary removal
    does nothing until the node itself confirms.
    """
    ref = state.node_ref()
    if ref is None:
        return

    def carry_out(mode: Any) -> None:
        force = mode == REMOVE_FORCE

        def task() -> str:
            view = state.hub.remove(ref, force=force)
            state.reload()
            if view.acknowledged:
                how = "the node confirmed and goes back to joining"
            else:
                how = "not confirmed — it may come back as PENDING"
            return f"{ref}: removed, {view.bindings} binding(s) with it — {how}"

        state.worker.submit(task)

    state.overlay = overlay.Choose(
        title="Remove from the hub?",
        subject=f"{ref} gets a factory reset; the hub forgets it, its entry and bindings",
        options=(REMOVE_WAIT, REMOVE_FORCE),
        on_accept=carry_out,
        note="force is for a module that is dead or already off the wall",
        accept="remove",
    )


def _enumerate(state: State) -> None:
    ref = state.node_ref()
    if ref is None:
        return

    def task() -> str:
        view = state.hub.enumerate(ref)
        return f"{ref}: enumeration {'started' if view.started else 'did not start'}"

    state.worker.submit(task)


def _block(state: State) -> None:
    """`B` toggles: a blocked node is unblocked. Goes to the file, like every decision
    about the installation (06-cykl-zycia-urzadzenia.md §8)."""
    selected = state.selected()
    if state.section is Section.NODES and isinstance(selected, NodeSummary):
        ref, blocked = selected.alias or selected.uid, selected.blocked
    elif state.section is Section.JOINING and isinstance(selected, JoinView) and selected.uid:
        ref, blocked = selected.uid, selected.state == "BLOCKED"
    else:
        state.say("! choose a node — in joining, one the hub has read")
        return

    def task() -> str:
        if blocked:
            state.hub.unblock(ref)
        else:
            state.hub.block(ref)
        state.reload()
        return f"{ref}: {'unblocked — reading it again' if blocked else 'blocked'}"

    state.worker.submit(task)


def _reconcile(state: State) -> None:
    """`R` means the same in two places: check this node’s agreement now."""
    ref = _ref_to_reconcile(state)
    if ref is None:
        state.say("! it is not clear which node to reconcile")
        return

    def task() -> str:
        view = state.hub.reconcile(ref)
        state.reload()
        return f"{ref}: reconciling {'started' if view.started else 'had nothing to check'}"

    state.worker.submit(task)


def _ref_to_reconcile(state: State) -> str | None:
    if state.section is Section.DIFF:
        selected = state.selected()
        if isinstance(selected, DiffRow):
            return selected.alias or selected.uid
        return None
    return state.node_ref()


def _filter(state: State) -> None:
    def apply(value: Any) -> None:
        state.filter = str(value)
        state.cursor[state.section] = 0
        state.dirty = True

    state.overlay = overlay.Edit(
        title="List filter",
        subject=f"section: {state.section.value}",
        hint="shows only the rows containing the typed text",
        on_accept=apply,
        buffer=state.filter,
        note="Esc clears the filter",
    )


def _help(state: State) -> None:
    state.overlay = overlay.Message(title="Keys", body=HELP)


# --- actions on an attribute ------------------------------------------------------


def _attribute(state: State) -> tuple[str, int, AttributeView] | None:
    """The node, endpoint and attribute under the cursor — or `None` when the cursor is
    elsewhere."""
    ref = state.node_ref()
    selected = state.detail_selected()
    if ref is None or state.endpoint is None or not isinstance(selected, AttributeView):
        return None
    return ref, state.endpoint, selected


def _edit(state: State) -> None:
    """A write to the **file**. It reaches the node by reconciling, not from here."""
    target = _attribute(state)
    if target is None:
        state.say("! enter an endpoint and choose an attribute")
        return
    ref, endpoint, meta = target
    if not meta.configurable:
        state.say(f"! {meta.name} is {'state' if 'W' not in meta.access else 'a working setting'}")
        return

    def save(value: Any) -> None:
        state.worker.submit(lambda: _write_to_file(state, ref, endpoint, meta.name, value))

    state.overlay = _question(
        title="Edit in the file",
        subject=f"{ref} · ep{endpoint} · {meta.name}",
        type_name=meta.type,
        choices=meta.choices,
        now=meta.wanted_label or _text(meta.wanted),
        on_accept=save,
        note="The write goes to the FILE. It reaches the node by reconciling.",
    )


def _set_on_node(state: State) -> None:
    """A working setting — **straight to the node**, it does not touch the file."""
    target = _attribute(state)
    if target is None or "W" not in target[2].access:
        state.say("! this attribute cannot be written on the node")
        return
    ref, endpoint, meta = target

    def send(value: Any) -> None:
        state.worker.submit(
            lambda: (
                f"{meta.name} = {value}: {state.hub.write(ref, endpoint, meta.name, value).result}"
            )
        )

    state.overlay = _question(
        title="Setting on the node",
        subject=f"{ref} · ep{endpoint} · {meta.name}",
        type_name=meta.type,
        choices=meta.choices,
        now=meta.label or _text(meta.value),
        on_accept=send,
        note="It goes STRAIGHT to the node. It does not touch the file.",
    )


def _remove(state: State) -> None:
    target = _attribute(state)
    if target is None or not target[2].configurable:
        return
    ref, endpoint, meta = target
    path = f"nodes/{_uid(state, ref)}/endpoints/{endpoint}/{meta.name}"
    state.worker.submit(lambda: _removal(state, path))


def _read(state: State) -> None:
    target = _attribute(state)
    if target is None or "R" not in target[2].access:
        return
    ref, endpoint, meta = target

    def task() -> str:
        result = state.hub.read(ref, endpoint, meta.name)
        state.reload()
        return f"{meta.name}: {result.result} {result.data}"

    state.worker.submit(task)


def _adopt(state: State) -> None:
    """A value from the node **into the file** — the only place where something goes this
    way."""
    target = _attribute(state)
    if target is not None:
        ref, endpoint, meta = target[0], target[1], target[2].name
    elif state.section is Section.DIFF and isinstance(state.selected(), DiffRow):
        row = state.selected()
        ref, endpoint, meta = row.alias or row.uid, row.endpoint, row.attribute
    else:
        state.say("! it is not clear which value to adopt")
        return

    def task() -> str:
        state.hub.config_adopt(ref, endpoint, meta, etag=state.hub.config().etag)
        state.reload()
        return f"{meta}: the node’s value copied INTO THE FILE"

    state.worker.submit(task)


def _write_to_file(state: State, ref: str, endpoint: int, attribute: str, value: Any) -> str:
    """Through `config_set_attribute`, because it adds `type` when the file has none yet."""
    state.hub.config_set_attribute(ref, endpoint, attribute, value, etag=state.hub.config().etag)
    state.reload()
    return f"{ref} ep{endpoint} {attribute} = {value} — saved in the file"


def _removal(state: State, path: str) -> str:
    state.hub.config_delete(path, etag=state.hub.config().etag)
    state.reload()
    return f"{path} — removed from the file"


def _refresh(state: State) -> None:
    def task() -> str:
        state.reload()
        return "refreshed"

    state.worker.submit(task)


def _pause(state: State) -> None:
    state.feed.paused = not state.feed.paused


def _command_to_endpoint(state: State) -> None:
    """A command goes to the endpoint under the cursor — and when the cursor is higher, to
    whichever one takes anything at all."""
    ref = state.node_ref()
    if ref is None:
        state.say("! choose a node")
        return
    selected = state.detail_selected()
    endpoint = selected if isinstance(selected, EndpointView) else None
    if endpoint is None and state.endpoint is not None and state.snapshot.node is not None:
        endpoint = next((e for e in state.snapshot.node.detail if e.number == state.endpoint), None)
    if endpoint is None:
        endpoint = state.output_endpoint()
    if endpoint is None:
        state.say("! it is not clear which endpoint")
        return
    actions.send_command(state, ref, endpoint)


def _name(state: State) -> None:
    ref = state.node_ref()
    if ref is None:
        return
    actions.name_node(state, ref, _uid(state, ref), "name")


def _alias(state: State) -> None:
    ref = state.node_ref()
    if ref is None:
        return
    actions.name_node(state, ref, _uid(state, ref), "alias")


def _add(state: State) -> None:
    if state.section is Section.BINDINGS:
        actions.add_binding(state)


def _remove_binding(state: State) -> None:
    if state.section is not Section.BINDINGS:
        return
    selected = state.selected()
    if not isinstance(selected, tuple):
        state.say("! no binding is selected")
        return
    binding = selected[0]
    if not binding.source_alias:
        state.say("! the source has no alias — it cannot be named in the file")
        return
    actions.remove_binding(state, binding.source_alias, binding.source_endpoint)


_LETTERS: Final[dict[int, Callable[[State], None]]] = {
    ord("t"): lambda s: _toggle(s, "Toggle"),
    ord("o"): lambda s: _toggle(s, "On"),
    ord("f"): lambda s: _toggle(s, "Off"),
    ord("i"): _identify,
    ord("a"): _accept,
    ord("y"): _admit,
    ord("X"): _x,
    ord("E"): _enumerate,
    ord("B"): _block,
    ord("R"): _reconcile,
    ord("e"): _edit,
    ord("w"): _set_on_node,
    ord("u"): _remove,
    ord("g"): _read,
    ord("p"): _adopt,
    ord("r"): _refresh,
    ord("c"): _command_to_endpoint,
    ord("n"): _name,
    ord("N"): _alias,
    ord("+"): _add,
    ord("-"): _remove_binding,
    ord("/"): _filter,
    ord("?"): _help,
    ord(" "): _pause,
}


# --- small change -----------------------------------------------------------------


CHOICE_TYPES: Final[dict[str, tuple[str, ...]]] = {
    "bool": ("yes", "no"),
}
"""Types that are **chosen from a list, not typed**.

`bool` looks innocent and is a trap when typed: `true`, `True`, `1`, `on`, `yes` — a
person has no way of knowing which one this hub wants. A list removes the question."""

RANGES: Final[dict[str, str]] = {
    "u8": "0…255",
    "u16": "0…65535",
    "u32": "0…4294967295",
    "i8": "−128…127",
    "i16": "−32768…32767",
    "i32": "−2147483648…2147483647",
    "map8": "a bit mask, 0…255",
    "map16": "a bit mask, 0…65535",
}


def _question(
    *,
    title: str,
    subject: str,
    type_name: str,
    choices: tuple[str, ...],
    now: str,
    on_accept: Callable[[Any], None],
    note: str,
) -> overlay.Overlay:
    """A window fitted to the **kind of value**, not one for everything.

    Named and boolean values go to a list, because there is nothing to guess there. A
    number goes to typing, but **with its range written out** — otherwise one has to
    know in advance whether `u16` is 0...65535 or something else.
    """
    options = choices or CHOICE_TYPES.get(type_name, ())
    if options:
        return overlay.Choose(
            title=title,
            subject=f"{subject}\nnow: {now or '—'}",
            options=options,
            on_accept=lambda w: on_accept(_value(w)),
            cursor=options.index(now) if now in options else 0,
            note=note,
        )
    span = RANGES.get(type_name)
    return overlay.Edit(
        title=title,
        subject=subject,
        hint=f"{type_name}" + (f" · {span}" if span else "") + f" · now: {now or '—'}",
        on_accept=lambda w: on_accept(_value(w)),
        buffer=now if now and now != "—" else "",
        note=note,
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _uid(state: State, ref: str) -> str:
    """The write path addresses the node by `Uid`: an alias is a label and may be
    changed."""
    selected = state.selected()
    if isinstance(selected, NodeSummary):
        return selected.uid
    node = state.snapshot.node
    return node.uid if node else ref


def _value(value: Any) -> Any:
    """`30` is to land in the file as a number, not as `"30"` — a person reads the file."""
    text = str(value).strip()
    if text.lower() in ("true", "yes", "on"):
        return True
    if text.lower() in ("false", "no", "off"):
        return False
    try:
        return int(text, 0)
    except ValueError:
        return text


def main(hub: Hub) -> int:
    try:
        return run(hub)
    except ApiError as exc:
        print(f"cannot connect: {exc.message}")
        return 2


__all__ = ["HELP", "key", "main", "run"]
