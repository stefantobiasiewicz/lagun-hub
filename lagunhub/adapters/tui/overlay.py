"""Windows on top: editing a value and confirming something irreversible.

This is the only place in the whole view where the keyboard is unavoidable — and it is
good that there is **one** of them. Everything else is done with arrows.

Two kinds of editing, because there are two kinds of value
([`protocol/endpoints/enums.py`](../../protocol/endpoints/enums.py)):

| | |
|---|---|
| a number | typed in, with the range printed above the field |
| named | **chosen from a list**, because names are not guessed — `choices` comes from the hub |

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


@dataclass
class Edit:
    """Typing a value in. `Esc` cancels, `⏎` confirms."""

    title: str
    subject: str
    hint: str
    on_accept: Accept
    buffer: str = ""
    note: str = ""

    def lines(self) -> Sequence[str]:
        return [
            self.subject,
            "",
            self.hint,
            "",
            f"  value:  {self.buffer}▏",
            "",
            *([self.note] if self.note else []),
            *([""] if self.note else []),
            "⏎ save      Esc cancel",
        ]

    def key(self, key_sim: int) -> bool:
        """`False` means "close the window"."""
        if key_sim in (27,):
            return False
        if key_sim in (10, 13):
            self.on_accept(self.buffer)
            return False
        if key_sim in (263, 127, 8):
            self.buffer = self.buffer[:-1]
        elif 32 <= key_sim < 127:
            self.buffer += chr(key_sim)
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


Overlay = Edit | Choose | Confirm | Message

__all__ = ["Choose", "Confirm", "Edit", "Message", "Overlay"]
