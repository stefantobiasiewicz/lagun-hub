"""Windows on top: editing a value and confirming something irreversible.

This is the only place in the whole view where the keyboard is unavoidable — and it is
good that there is **one** of them. Everything else is done with arrows.

Two kinds of editing, because there are two kinds of value
([`protocol/endpoints/enums.py`](../../protocol/endpoints/enums.py)):

| | |
|---|---|
| a number | typed in, with the range printed above the field |
| named | **chosen from a list**, because names are not guessed — `choices` comes from the hub |

Typing is line editing, not appending: `←` `→` move the cursor, `Home` `End` jump, a
key types where the cursor is, `Backspace` and `Delete` take the character before and
under it (`_Line`). A command with several arguments is a `Form`, one field a line —
`↑` `↓` or `Tab` between them — not one `name=value name=value` line: changing the
blue of a `SetColor` meant retyping the whole colour (2026-10-17, DEVICE-03).

**A confirmation never asks for typing** (zadania/zrobione/CR-07.md). It names what will
happen and to what, and waits for `⏎`; `Esc` backs out, any other key does nothing. The
guard against an accident is two different keys — `X`, `-` or `u` to open it, `⏎` to
carry it out — and a window that says what it is about, the same guard removing a node
has had since FEAT-04. Retyping a name the window already shows was rejected: it is a
typing exercise, not a decision, and a ShortId in hex is easy to get wrong.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

Accept = Callable[[Any], None]

# curses key codes, as numbers: this module does not import curses, so it is tested
# without a terminal.
KEY_DOWN, KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_HOME = 258, 259, 260, 261, 262
KEY_BACKSPACE, KEY_DC, KEY_BTAB, KEY_END = 263, 330, 353, 360
TAB, ENTER, RETURN, ESC = 9, 10, 13, 27


@dataclass
class _Line:
    """One line of text with a cursor. `pos` None means "at the end" — where a window
    opens, so typing straight away appends as it always did."""

    text: str = ""
    pos: int | None = None

    @property
    def at(self) -> int:
        return len(self.text) if self.pos is None else min(self.pos, len(self.text))

    def key(self, key_sim: int) -> bool:
        """Edit by `key_sim`; `False` when it is not an editing key."""
        at = self.at
        if key_sim == KEY_LEFT:
            self.pos = max(0, at - 1)
        elif key_sim == KEY_RIGHT:
            self.pos = min(len(self.text), at + 1)
        elif key_sim == KEY_HOME:
            self.pos = 0
        elif key_sim == KEY_END:
            self.pos = len(self.text)
        elif key_sim in (KEY_BACKSPACE, 127, 8):
            if at > 0:
                self.text = self.text[: at - 1] + self.text[at:]
                self.pos = at - 1
        elif key_sim == KEY_DC:
            self.text = self.text[:at] + self.text[at + 1 :]
            self.pos = at
        elif 32 <= key_sim < 127:
            self.text = self.text[:at] + chr(key_sim) + self.text[at:]
            self.pos = at + 1
        else:
            return False
        return True

    def shown(self) -> str:
        """The text with the cursor drawn in it."""
        return self.text[: self.at] + "▏" + self.text[self.at :]


@dataclass
class Edit:
    """Typing a value in. `Esc` cancels, `⏎` confirms."""

    title: str
    subject: str
    hint: str
    on_accept: Accept
    buffer: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        self._line = _Line(self.buffer)

    def lines(self) -> Sequence[str]:
        return [
            self.subject,
            "",
            self.hint,
            "",
            f"  value:  {self._line.shown()}",
            "",
            *([self.note] if self.note else []),
            *([""] if self.note else []),
            "⏎ save      Esc cancel",
        ]

    def key(self, key_sim: int) -> bool:
        """`False` means "close the window"."""
        if key_sim == ESC:
            return False
        if key_sim in (ENTER, RETURN):
            self.on_accept(self.buffer)
            return False
        self._line.key(key_sim)
        self.buffer = self._line.text
        return True


@dataclass
class Field:
    """One field of a `Form`: its name, what it takes, and what it holds."""

    name: str
    hint: str = ""
    value: str = ""


@dataclass
class Form:
    """Several values, one field a line — the arguments of a command. `↑` `↓` or `Tab`
    pick the field, the rest edits it as `Edit` does; `⏎` accepts all of them at once,
    as `{name: text}`, `Esc` cancels."""

    title: str
    subject: str
    fields: tuple[Field, ...]
    on_accept: Accept
    note: str = ""
    accept: str = "send"
    cursor: int = 0

    def __post_init__(self) -> None:
        self._lines = [_Line(f.value) for f in self.fields]

    @property
    def values(self) -> dict[str, str]:
        return {f.name: line.text for f, line in zip(self.fields, self._lines, strict=True)}

    def lines(self) -> Sequence[str]:
        name_w = max((len(f.name) for f in self.fields), default=0)
        value_w = max((len(line.text) for line in self._lines), default=0) + 1
        rows = []
        for i, (f, line) in enumerate(zip(self.fields, self._lines, strict=True)):
            here = i == self.cursor
            value = line.shown() if here else line.text
            mark = "▸" if here else " "
            rows.append(f"  {mark} {f.name:<{name_w}}  {value:<{value_w}}  {f.hint}".rstrip())
        return [
            self.subject,
            "",
            *rows,
            "",
            *([self.note] if self.note else []),
            *([""] if self.note else []),
            f"↑↓ Tab field  ←→ cursor   ⏎ {self.accept}   Esc cancel",
        ]

    def key(self, key_sim: int) -> bool:
        if key_sim == ESC:
            return False
        if key_sim in (ENTER, RETURN):
            self.on_accept(self.values)
            return False
        if key_sim in (KEY_DOWN, TAB):
            self.cursor = (self.cursor + 1) % len(self.fields)
        elif key_sim in (KEY_UP, KEY_BTAB):
            self.cursor = (self.cursor - 1) % len(self.fields)
        else:
            self._lines[self.cursor].key(key_sim)
        return True


@dataclass
class Choose:
    """A choice from a list. Names are not guessed — `choices` comes from the hub."""

    title: str
    subject: str
    options: tuple[str, ...]
    on_accept: Accept
    cursor: int = 0
    note: str = ""
    accept: str = "save"
    """What `⏎` does, as the last line says it — "remove" when the choice is itself the
    confirmation."""

    def lines(self) -> Sequence[str]:
        return [
            self.subject,
            "",
            *[
                f"  {'▸' if i == self.cursor else ' '} {option}"
                for i, option in enumerate(self.options)
            ],
            "",
            *([self.note] if self.note else []),
            *([""] if self.note else []),
            f"⏎ {self.accept}      Esc cancel",
        ]

    def key(self, key_sim: int) -> bool:
        if key_sim == 27:
            return False
        if key_sim in (10, 13):
            if self.options:
                self.on_accept(self.options[self.cursor])
            return False
        if key_sim == 259:  # KEY_UP
            self.cursor = max(0, self.cursor - 1)
        elif key_sim == 258:  # KEY_DOWN
            self.cursor = min(len(self.options) - 1, self.cursor + 1)
        return True


@dataclass
class Confirm:
    """Something irreversible, named, before it happens. `⏎` carries it out, `Esc`
    cancels, nothing else does anything."""

    title: str
    lines_before: tuple[str, ...]
    on_accept: Accept
    accept: str = "carry it out"
    """What `⏎` does, in the last line — `reset`, `remove` — as `Choose.accept`."""

    def lines(self) -> Sequence[str]:
        return [*self.lines_before, "", f"⏎ {self.accept}      Esc cancel"]

    def key(self, key_sim: int) -> bool:
        if key_sim == 27:
            return False
        if key_sim in (10, 13):
            self.on_accept(None)
            return False
        return True


@dataclass
class Message:
    """A read-only window. Any key closes it."""

    title: str
    body: tuple[str, ...]

    def lines(self) -> Sequence[str]:
        return [*self.body, "", "any key closes it"]

    def key(self, key_sim: int) -> bool:
        del key_sim
        return False


Overlay = Edit | Form | Choose | Confirm | Message

__all__ = ["Choose", "Confirm", "Edit", "Field", "Form", "Message", "Overlay"]
