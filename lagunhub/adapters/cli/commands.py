"""The shell commands’ content — **data, not strings**.

The split costs one file and saves rewriting everything the moment a second way of
showing the same things arrives: the full-screen view is to get a `NodeView`, not a
formatted table row it would have to take apart again
([`ARCHITEKTURA.md`](../../../ARCHITEKTURA.md) §10.3).

It is tested without a process, without a port and without a terminal: `Hub` is a
protocol, so in a test anything with those methods will do.

One distinction is visible here outright, because in an installation it can hurt:

| | |
|---|---|
| `set`   | **configuration** — goes to the file, and reaches the node by reconciling |
| `write` | **a working setting** — goes straight to the node and does not touch the file |

A configuration write straight to a node would be undone at the first agreement check
([`KONFIGURACJA.md`](../../../KONFIGURACJA.md) §11.1).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Final, Protocol

from lagunhub.api import (
    ActionView,
    ApiError,
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
    Problem,
    ReconcileView,
    TrafficView,
)


class Hub(Protocol):
    """What the shell needs from the hub. Both `HubApi` and `HubClient` satisfy it."""

    def info(self) -> HubInfo: ...
    def nodes(self) -> tuple[NodeSummary, ...]: ...
    def node(self, ref: str) -> NodeView: ...
    def endpoint(self, ref: str, number: int) -> EndpointView: ...
    def bindings(self) -> tuple[BindingView, ...]: ...
    def issues(self) -> tuple[str, ...]: ...
    def health(self) -> HealthView: ...
    def commissioning(self) -> CommissioningView: ...
    def diff(self) -> tuple[DiffRow, ...]: ...
    def command(
        self, ref: str, endpoint: int, name: str, args: Mapping[str, Any] | None = None
    ) -> ActionView: ...
    def write(self, ref: str, endpoint: int, attribute: str, value: Any) -> ActionView: ...
    def read(self, ref: str, endpoint: int, attribute: str) -> ActionView: ...
    def identify(self, ref: str, seconds: int = 5) -> ActionView: ...
    def accept_new(self, seconds: float = 900.0) -> CommissioningView: ...
    def config(self) -> ConfigView: ...
    def config_set(self, path: str, value: Any, *, etag: str) -> ConfigView: ...
    def config_delete(self, path: str, *, etag: str) -> ConfigView: ...
    def reconcile(self, ref: str) -> ReconcileView: ...
    def config_set_attribute(
        self, ref: str, endpoint: int, attribute: str, value: Any, *, etag: str
    ) -> ConfigView: ...
    def config_adopt(self, ref: str, endpoint: int, attribute: str, *, etag: str) -> ConfigView: ...
    def stream(self) -> AbstractContextManager[Iterable[EventView]]:
        """Live events. The shell only iterates over them, so this is enough — and that is
        what lets both an in-process hub and an HTTP client satisfy it."""
        ...

    def frames(self) -> AbstractContextManager[Iterable[TrafficView]]:
        """Live traffic on the bus."""
        ...


@dataclass(frozen=True, slots=True)
class Result:
    """What a command returned. `kind` tells whoever draws it what they are looking at."""

    kind: str
    data: Any = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    run: Callable[[Hub, Sequence[str]], Result]
    usage: str
    help: str


# --- commands --------------------------------------------------------------------


def _nodes(hub: Hub, args: Sequence[str]) -> Result:
    _no_args(args, "nodes")
    return Result("nodes", hub.nodes())


def _show(hub: Hub, args: Sequence[str]) -> Result:
    return Result("node", hub.node(_arg(args, 0, "show", "node")))


def _ep(hub: Hub, args: Sequence[str]) -> Result:
    ref = _arg(args, 0, "ep", "node")
    return Result("endpoint", hub.endpoint(ref, _endpoint(_arg(args, 1, "ep", "endpoint"))))


def _cmd(hub: Hub, args: Sequence[str]) -> Result:
    ref = _arg(args, 0, "cmd", "node")
    number = _endpoint(_arg(args, 1, "cmd", "endpoint"))
    name = _arg(args, 2, "cmd", "command")
    return Result("action", hub.command(ref, number, name, _kwargs(args[3:])), f"{name} → {ref}")


def _write(hub: Hub, args: Sequence[str]) -> Result:
    ref = _arg(args, 0, "write", "node")
    number = _endpoint(_arg(args, 1, "write", "endpoint"))
    name = _arg(args, 2, "write", "attribute")
    value = _value(_arg(args, 3, "write", "value"))
    return Result("action", hub.write(ref, number, name, value), f"{name} = {value} → {ref}")


def _get(hub: Hub, args: Sequence[str]) -> Result:
    ref = _arg(args, 0, "get", "node")
    number = _endpoint(_arg(args, 1, "get", "endpoint"))
    name = _arg(args, 2, "get", "attribute")
    return Result("action", hub.read(ref, number, name), f"{name} ← {ref}")


def _set(hub: Hub, args: Sequence[str]) -> Result:
    """A write to the **file**. It reaches the node by reconciling, not from here."""
    ref = _arg(args, 0, "set", "node")
    number = _endpoint(_arg(args, 1, "set", "endpoint"))
    name = _arg(args, 2, "set", "attribute")
    value = _value(_arg(args, 3, "set", "value"))
    view = hub.config_set_attribute(ref, number, name, value, etag=hub.config().etag)
    return Result("config", view, f"{ref} ep{number} {name} = {value} — saved in the file")


def _unset(hub: Hub, args: Sequence[str]) -> Result:
    ref = _arg(args, 0, "unset", "node")
    number = _endpoint(_arg(args, 1, "unset", "endpoint"))
    name = _arg(args, 2, "unset", "attribute")
    path = _config_path(hub, ref, number, name)
    return Result("config", hub.config_delete(path, etag=hub.config().etag), f"{path} removed")


def _identify(hub: Hub, args: Sequence[str]) -> Result:
    ref = _arg(args, 0, "identify", "node")
    seconds = int(_duration(args[1])) if len(args) > 1 else 5
    return Result("action", hub.identify(ref, seconds), f"{ref} signals for {seconds} s")


def _accept(hub: Hub, args: Sequence[str]) -> Result:
    seconds = _duration(args[0]) if args else 900.0
    view = hub.accept_new(seconds)
    return Result("commissioning", view, f"joining open for {_human(seconds)}")


def _commissioning(hub: Hub, args: Sequence[str]) -> Result:
    _no_args(args, "joining")
    return Result("commissioning", hub.commissioning())


def _diff(hub: Hub, args: Sequence[str]) -> Result:
    _no_args(args, "diff")
    return Result("diff", hub.diff())


def _issues(hub: Hub, args: Sequence[str]) -> Result:
    _no_args(args, "issues")
    return Result("issues", hub.issues())


def _health(hub: Hub, args: Sequence[str]) -> Result:
    _no_args(args, "health")
    return Result("health", hub.health())


def _bindings(hub: Hub, args: Sequence[str]) -> Result:
    _no_args(args, "bindings")
    return Result("bindings", hub.bindings())


def _config(hub: Hub, args: Sequence[str]) -> Result:
    _no_args(args, "config")
    return Result("config", hub.config())


COMMANDS: Final[tuple[Command, ...]] = (
    Command("nodes", _nodes, "nodes", "what the hub knows about the installation"),
    Command("show", _show, "show <node>", "the model and state of one node"),
    Command("ep", _ep, "ep <node> <nr>", "one endpoint with all its attributes"),
    Command("cmd", _cmd, "cmd <node> <nr> <command> [k=v]", "a command to the node"),
    Command("get", _get, "get <node> <nr> <attribute>", "a read STRAIGHT from the node"),
    Command("write", _write, "write <node> <nr> <attribute> <value>", "a working setting"),
    Command("set", _set, "set <node> <nr> <attribute> <value>", "configuration — to the FILE"),
    Command("unset", _unset, "unset <node> <nr> <attribute>", "remove from the configuration"),
    Command("identify", _identify, "identify <node> [time]", "which box is it"),
    Command("accept", _accept, "accept [time]", "open joining for new nodes"),
    Command("joining", _commissioning, "joining", "who is announcing and what they got"),
    Command("diff", _diff, "diff", "how the installation differs from the file"),
    Command("issues", _issues, "issues", "what needs the operator's decision"),
    Command("health", _health, "health", "latencies, counters, buses"),
    Command("bindings", _bindings, "bindings", "the bindings and whether they have a target"),
    Command("config", _config, "config", "the configuration as it stands in the files"),
)

BY_NAME: Final[dict[str, Command]] = {command.name: command for command in COMMANDS}


def run(hub: Hub, line: str) -> Result:
    """Run a command line. Raises `ApiError` — the shell draws it."""
    parts = line.split()
    if not parts:
        return Result("none")
    command = BY_NAME.get(parts[0])
    if command is None:
        familiar = ", ".join(sorted(BY_NAME))
        raise ApiError(
            Problem.NOT_FOUND, f"there is no command {parts[0]!r}; there are: {familiar}"
        )
    return command.run(hub, parts[1:])


# --- taking the arguments apart --------------------------------------------------

DURATION: Final = re.compile(r"^(\d+(?:\.\d+)?)([smhSMH]?)$")
UNITS: Final = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0}


def _arg(args: Sequence[str], index: int, command: str, what: str) -> str:
    if index >= len(args):
        usage = BY_NAME[command].usage
        raise ApiError(Problem.INVALID, f"missing: {what}. Usage: {usage}")
    return args[index]


def _no_args(args: Sequence[str], command: str) -> None:
    if args:
        raise ApiError(Problem.INVALID, f"{command} takes no arguments")


def _endpoint(text: str) -> int:
    """`4` and `ep4` mean the same — because the hub prints `ep4`, so a person will copy
    that."""
    bare = text[2:] if text.lower().startswith("ep") else text
    try:
        return int(bare, 0)
    except ValueError:
        raise ApiError(Problem.INVALID, f"{text!r} is not an endpoint number") from None


def _value(text: str) -> Any:
    """A number stays a number, `true`/`false` a boolean, the rest a string.

    It matters for `set`: the value goes into a YAML file, so `30` stored as a string
    would leave `"30"` in it — valid, but ugly and misleading at the next reading.
    """
    if text.lower() in ("true", "on", "yes"):
        return True
    if text.lower() in ("false", "off", "no"):
        return False
    try:
        return int(text, 0)
    except ValueError:
        return text


def _kwargs(args: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for para in args:
        if "=" not in para:
            raise ApiError(Problem.INVALID, f"a command argument is written `name=value`: {para!r}")
        name, _, value = para.partition("=")
        out[name] = _value(value)
    return out


def _duration(text: str) -> float:
    match = DURATION.match(text)
    if match is None:
        raise ApiError(Problem.INVALID, f"{text!r} is not a time; e.g. 90, 15m, 1h")
    return float(match.group(1)) * UNITS[match.group(2).lower()]


def _human(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.0f} h"
    if seconds >= 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f} s"


def _config_path(hub: Hub, ref: str, endpoint: int, attribute: str) -> str:
    """A path in the document. A node appears in it under its `Uid`, not its alias — an
    alias is a label and may be changed
    ([`KONFIGURACJA.md`](../../../KONFIGURACJA.md) §11.2)."""
    return f"nodes/{hub.node(ref).uid}/endpoints/{endpoint}/{attribute}"


__all__ = ["BY_NAME", "COMMANDS", "Command", "Hub", "Result", "run"]
