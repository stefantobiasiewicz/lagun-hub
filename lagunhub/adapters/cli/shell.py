"""The hub shell — the only form the CLI takes.

There are no one-shot commands and there will not be until somebody shows they miss
them: against a remote hub, `lagun-hub nodes` is `curl` with nicer formatting, and what
the shell gives beyond that needs a session longer than one command
([`ARCHITEKTURA.md`](../../../ARCHITEKTURA.md) §10.3).

**Completion is the test of whether the hub API is any good.** Aliases, endpoint numbers,
command names, attribute names and their permitted values — the shell takes all of it
from the hub’s replies and **knows nothing about the `Output` type**. Whatever cannot
be completed is something the API does not expose and should.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Sequence
from typing import Any, Final

from lagunhub.adapters.cli import render
from lagunhub.adapters.cli.commands import BY_NAME, Hub, Result, run
from lagunhub.api import ApiError, EndpointView, Problem

try:
    import readline as _readline
except ImportError:  # pragma: no cover — Windows has no readline
    _readline = None  # type: ignore[assignment]

readline: Any = _readline
"""`Any`, because on Windows this module does not exist — and the shell is then to work
without completion rather than fail to start."""

PROMPT: Final = "lagun> "
CACHE_TTL: Final = 5.0
"""How long the shell trusts its own node list while completing. Tab is to be
instantaneous, and an installation does not change every second."""

ARGS: Final[dict[str, tuple[str, ...]]] = {
    "show": ("node",),
    "ep": ("node", "endpoint"),
    "cmd": ("node", "endpoint", "command"),
    "get": ("node", "endpoint", "attribute"),
    "write": ("node", "endpoint", "attribute", "value"),
    "set": ("node", "endpoint", "attribute", "value"),
    "unset": ("node", "endpoint", "attribute"),
    "identify": ("node",),
}
"""What each argument is. Completion comes from here and from nothing else."""

BUILTINS: Final[dict[str, str]] = {
    "help": "the list of commands",
    "feed": "feed on|off — an event feed under the prompt",
    "watch": "an event stream until Ctrl-C",
    "quit": "quit (Ctrl-D too)",
}


class Shell:
    def __init__(self, hub: Hub, *, out: Any = None) -> None:
        self.hub = hub
        self.out = out or sys.stdout
        self.feed = False
        self._tty = bool(getattr(self.out, "isatty", bool)())
        """Whether there is a terminal on the other side. Control sequences sent to a file or
        a pipe would stay in it as `[2K` — rubbish in a log."""

        self._catalog = _Catalog(hub)
        self._feed_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._at_prompt = False

    # --- the loop ----------------------------------------------------------------

    def loop(self) -> int:
        self._install_completer()
        self._say(render.render(Result("info", self.hub.info())))
        self._say("`help` lists the commands, Ctrl-D quits")
        while True:
            try:
                self._at_prompt = True
                line = input(PROMPT)
            except EOFError:
                self._print("")
                break
            except KeyboardInterrupt:
                self._print("")  # Ctrl-C clears the line, it does not end the session
                continue
            finally:
                self._at_prompt = False
            if not self.execute(line):
                break
        self._stop_feed()
        return 0

    def execute(self, line: str) -> bool:
        """Run a line. `False` means "we are done"."""
        text = line.strip()
        if not text:
            return True
        parts = text.split()
        builtin_ones = self._builtin(parts[0], parts[1:])
        if builtin_ones is not None:
            return builtin_ones
        try:
            self._say(render.render(run(self.hub, text)))
        except ApiError as exc:
            self._say(_problem(exc))
        return True

    def _builtin(self, name: str, args: Sequence[str]) -> bool | None:
        if name in ("quit", "exit"):
            return False
        if name == "help":
            self._say(_help())
            return True
        if name == "feed":
            self._set_feed(args)
            return True
        if name == "watch":
            self._watch()
            return True
        return None

    # --- the feed ----------------------------------------------------------------

    def _set_feed(self, args: Sequence[str]) -> None:
        wanted = not self.feed if not args else args[0].lower() in ("on", "1", "yes")
        if wanted == self.feed:
            self._say(f"the feed is already {'on' if wanted else 'off'}")
            return
        self.feed = wanted
        if wanted:
            self._start_feed()
            self._say("feed on — `feed off` turns it off")
        else:
            self._stop_feed()
            self._say("feed off")

    def _start_feed(self) -> None:
        self._stop.clear()
        self._feed_thread = threading.Thread(target=self._pump, name="feed", daemon=True)
        self._feed_thread.start()

    def _stop_feed(self) -> None:
        self._stop.set()
        self.feed = False

    def _pump(self) -> None:
        try:
            with self.hub.stream() as events:
                for item in events:
                    if self._stop.is_set():
                        return
                    self._emit(render.event(item))
        except (ApiError, OSError) as exc:
            self._emit(f"  ! the feed broke off: {exc}")

    def _watch(self) -> None:
        """A stream without a prompt, until `Ctrl-C`. Then there is nothing to fight over the
        line with."""
        self._say("event stream — Ctrl-C ends it")
        try:
            with self.hub.stream() as events:
                for item in events:
                    self._print(render.event(item))
        except KeyboardInterrupt:
            self._print("")
        except (ApiError, OSError) as exc:
            self._say(
                _problem(exc) if isinstance(exc, ApiError) else f"the stream broke off: {exc}"
            )

    def _emit(self, text: str) -> None:
        """Printing from under the prompt without eating it.

        The one thing in the shell that breaks by itself: traffic printed in the
        background overwrites the line a person is typing. We clear the line, print our
        own, and **restore the prompt together with whatever had been typed into it**
        ([`ARCHITEKTURA.md`](../../../ARCHITEKTURA.md) §10.3.1).
        """
        if not self._tty:
            self._print(text)
            return
        buffer = readline.get_line_buffer() if readline is not None and self._at_prompt else ""
        self.out.write("\r\x1b[2K" + text + "\n")
        if self._at_prompt:
            self.out.write(PROMPT + buffer)
        self.out.flush()

    def _say(self, text: str) -> None:
        if text:
            self._print(text)

    def _print(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()

    # --- completion ---------------------------------------------------------------

    def _install_completer(self) -> None:
        if readline is None:
            return
        readline.set_completer_delims(" \t\n")
        readline.set_completer(self._complete)
        readline.parse_and_bind("tab: complete")

    def _complete(self, text: str, state: int) -> str | None:
        try:
            candidates = self.candidates(readline.get_line_buffer(), text)
        except Exception:
            return None
        return candidates[state] if state < len(candidates) else None

    def candidates(self, line: str, prefix: str) -> list[str]:
        """What fits at this point in the line. Split out so it can be tested without a
        terminal — `readline` does not exist in a test."""
        parts = line.split()
        position = len(parts) if line.endswith(" ") else len(parts) - 1
        if position <= 0:
            return _matching(prefix, [*BY_NAME, *BUILTINS])
        kinds = ARGS.get(parts[0], ())
        if position - 1 >= len(kinds):
            return []
        return _matching(prefix, self._for(kinds[position - 1], parts))

    def _for(self, kind: str, parts: Sequence[str]) -> list[str]:
        if kind == "node":
            return self._catalog.refs()
        if len(parts) < 2:
            return []
        endpoints = self._catalog.endpoints(parts[1])
        if kind == "endpoint":
            return [f"ep{e.number}" for e in endpoints]
        endpoint = _find_endpoint(endpoints, parts[2] if len(parts) > 2 else "")
        if endpoint is None:
            return []
        if kind == "command":
            return [c.name for c in endpoint.commands if c.available]
        if kind == "attribute":
            return _attributes(endpoint, parts[0])
        return _values(endpoint, parts[3] if len(parts) > 3 else "")


class _Catalog:
    """What the shell knows about the installation **for the tab key**.

    A short memory, because completion is to be instantaneous and an installation does
    not change every second. A mistake costs one suggestion, not a decision.
    """

    def __init__(self, hub: Hub, ttl: float = CACHE_TTL) -> None:
        self.hub = hub
        self.ttl = ttl
        self._refs: tuple[list[str], float] = ([], 0.0)
        self._endpoints: dict[str, tuple[tuple[EndpointView, ...], float]] = {}

    def refs(self) -> list[str]:
        value, when = self._refs
        if time.monotonic() - when > self.ttl:
            try:
                value = [n.alias or n.uid for n in self.hub.nodes()]
            except ApiError:
                value = []
            self._refs = (value, time.monotonic())
        return value

    def endpoints(self, ref: str) -> tuple[EndpointView, ...]:
        value, when = self._endpoints.get(ref, ((), 0.0))
        if time.monotonic() - when > self.ttl:
            try:
                value = self.hub.node(ref).detail
            except ApiError:
                value = ()
            self._endpoints[ref] = (value, time.monotonic())
        return value


# --- small change ----------------------------------------------------------------


def _matching(prefix: str, candidates: Sequence[str]) -> list[str]:
    return sorted(k for k in candidates if k.startswith(prefix))


def _find_endpoint(endpoints: Sequence[EndpointView], text: str) -> EndpointView | None:
    bare = text[2:] if text.lower().startswith("ep") else text
    try:
        number = int(bare, 0)
    except ValueError:
        return None
    return next((e for e in endpoints if e.number == number), None)


def _attributes(endpoint: EndpointView, command: str) -> list[str]:
    """`set` is about **configuration**, so it suggests only what configuration is.

    The remaining attributes are state and working settings — suggesting them under
    `set` would lead straight into the error "this is state, not configuration"
    (`KONFIGURACJA.md` §7).
    """
    if command in ("set", "unset"):
        return [a.name for a in endpoint.attributes if a.configurable]
    if command == "write":
        return [a.name for a in endpoint.attributes if "W" in a.access]
    return [a.name for a in endpoint.attributes if "R" in a.access]


def _values(endpoint: EndpointView, attribute: str) -> list[str]:
    meta = next((a for a in endpoint.attributes if a.name == attribute), None)
    if meta is None:
        return []
    if meta.choices:
        return list(meta.choices)
    return ["true", "false"] if meta.type == "bool" else []


def _help() -> str:
    lines = ["commands:"]
    lines.extend(f"  {c.usage:<38}{c.help}" for c in BY_NAME.values())
    lines.append("")
    lines.extend(f"  {name:<38}{description}" for name, description in BUILTINS.items())
    lines.append("")
    lines.append("  set  goes to the FILE, and reaches the node by reconciling")
    lines.append("  write goes STRAIGHT to the node and does not touch the file")
    return "\n".join(lines)


def _problem(exc: ApiError) -> str:
    if exc.problem is Problem.UNAVAILABLE:
        return f"! {exc.message}"
    return f"! {exc.message}" + (f"  ({exc.result})" if exc.result else "")


__all__ = ["ARGS", "PROMPT", "Shell"]
