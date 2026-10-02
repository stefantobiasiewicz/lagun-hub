"""The hub client — **the only place on the client side that knows about HTTP**.

The full-screen view talks to a hub running as a service, so the transport has to live
somewhere. It lives here and nowhere else: the view receives an object that satisfies
`Hub` — the same operations as `HubApi` on the hub side — and has no way of learning
which side it is on. That is also what lets its tests run on a fake hub.

There used to be a second client over this one, the `lagun-hub shell` REPL; it was
removed, because the view covers every operation it had (`zadania/zrobione/CR-06.md`,
[`ARCHITEKTURA.md`](../../../ARCHITEKTURA.md) §10.3).

**Replies come back as the same frozen dataclasses the hub exposes.** That costs one
layer of assembly from JSON and gives something there would otherwise not be: a change
of a view’s shape in `api/views.py` breaks `mypy` in the client instead of breaking quietly for
a person at the terminal.

The transport is from the standard library — the same as the server, and for the same
reason
([`adapters/http/server.py`](../http/server.py)). The hub’s client has no business
adding dependencies when the server has none.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import fields, is_dataclass
from http.client import HTTPResponse
from pathlib import Path
from types import UnionType
from typing import Any, Final, Protocol, get_args, get_origin, get_type_hints
from urllib.parse import quote

from lagunhub.api import (
    ActionView,
    ApiError,
    BindingView,
    CommissioningView,
    ConfigView,
    DiffRow,
    EndpointView,
    EnumerateView,
    EventView,
    HealthView,
    HubInfo,
    NodeSummary,
    NodeView,
    Problem,
    ReconcileView,
    RemoveView,
    TrafficView,
)

TIMEOUT: Final = 5.0
TOKEN_ENV: Final = "LAGUN_TOKEN"

PROBLEMS: Final[dict[str, Problem]] = {problem.value: problem for problem in Problem}


class Hub(Protocol):
    """What a client needs from the hub. Both `HubApi` and `HubClient` satisfy it."""

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
    def remove(self, ref: str, *, force: bool = False) -> RemoveView: ...
    def enumerate(self, ref: str) -> EnumerateView: ...
    def block(self, ref: str) -> ConfigView: ...
    def unblock(self, ref: str) -> ConfigView: ...
    def accept_new(self, seconds: float = 900.0) -> CommissioningView: ...
    def admit(self, short_id: int) -> CommissioningView: ...
    def factory_reset(self, short_id: int) -> ActionView: ...
    def config(self) -> ConfigView: ...
    def config_set(self, path: str, value: Any, *, etag: str) -> ConfigView: ...
    def config_delete(self, path: str, *, etag: str) -> ConfigView: ...
    def reconcile(self, ref: str) -> ReconcileView: ...
    def config_set_attribute(
        self, ref: str, endpoint: int, attribute: str, value: Any, *, etag: str
    ) -> ConfigView: ...
    def config_adopt(self, ref: str, endpoint: int, attribute: str, *, etag: str) -> ConfigView: ...
    def stream(self) -> AbstractContextManager[Iterable[EventView]]:
        """Live events. A client only iterates over them, so this is enough — and that is
        what lets both an in-process hub and an HTTP client satisfy it.

        The view does not use it (its lists are polled, its feed is `frames()`); it stays
        so that `HubClient` mirrors the whole HTTP API and the conformance test keeps
        both sides honest (`zadania/zrobione/CR-06.md`, N5)."""
        ...

    def frames(self) -> AbstractContextManager[Iterable[TrafficView]]:
        """Live traffic on the bus."""
        ...


class HubClient:
    """The hub’s operations over HTTP. Shaped like `HubApi`."""

    def __init__(self, url: str, token: str, *, timeout: float = TIMEOUT) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # --- reading -----------------------------------------------------------------

    def info(self) -> HubInfo:
        return _build(HubInfo, self._call("GET", "/"))

    def nodes(self) -> tuple[NodeSummary, ...]:
        return self._many(NodeSummary, self._call("GET", "/nodes"))

    def node(self, ref: str) -> NodeView:
        return _build(NodeView, self._call("GET", f"/nodes/{_quote(ref)}"))

    def endpoint(self, ref: str, number: int) -> EndpointView:
        return _build(EndpointView, self._call("GET", f"/nodes/{_quote(ref)}/ep/{number}"))

    def bindings(self) -> tuple[BindingView, ...]:
        return self._many(BindingView, self._call("GET", "/bindings"))

    def issues(self) -> tuple[str, ...]:
        result = self._call("GET", "/issues")
        return tuple(str(item) for item in result) if isinstance(result, list) else ()

    def health(self) -> HealthView:
        return _build(HealthView, self._call("GET", "/health"))

    def commissioning(self) -> CommissioningView:
        return _build(CommissioningView, self._call("GET", "/commissioning"))

    def diff(self) -> tuple[DiffRow, ...]:
        return self._many(DiffRow, self._call("GET", "/diff"))

    # --- control -----------------------------------------------------------------

    def command(
        self, ref: str, endpoint: int, name: str, args: Mapping[str, Any] | None = None
    ) -> ActionView:
        return _build(
            ActionView,
            self._call(
                "POST",
                f"/nodes/{_quote(ref)}/ep/{endpoint}/commands/{_quote(name)}",
                {"args": dict(args or {})},
            ),
        )

    def write(self, ref: str, endpoint: int, attribute: str, value: Any) -> ActionView:
        return _build(
            ActionView,
            self._call(
                "PUT",
                f"/nodes/{_quote(ref)}/ep/{endpoint}/attributes/{_quote(attribute)}",
                {"value": value},
            ),
        )

    def read(self, ref: str, endpoint: int, attribute: str) -> ActionView:
        return _build(
            ActionView,
            self._call("GET", f"/nodes/{_quote(ref)}/ep/{endpoint}/attributes/{_quote(attribute)}"),
        )

    def identify(self, ref: str, seconds: int = 5) -> ActionView:
        return _build(
            ActionView, self._call("POST", f"/nodes/{_quote(ref)}/identify", {"seconds": seconds})
        )

    def remove(self, ref: str, *, force: bool = False) -> RemoveView:
        return _build(
            RemoveView, self._call("POST", f"/nodes/{_quote(ref)}/remove", {"force": force})
        )

    def enumerate(self, ref: str) -> EnumerateView:
        return _build(EnumerateView, self._call("POST", f"/nodes/{_quote(ref)}/enumerate"))

    def block(self, ref: str) -> ConfigView:
        return _build(ConfigView, self._call("POST", f"/nodes/{_quote(ref)}/block"))

    def unblock(self, ref: str) -> ConfigView:
        return _build(ConfigView, self._call("POST", f"/nodes/{_quote(ref)}/unblock"))

    def accept_new(self, seconds: float = 900.0) -> CommissioningView:
        return _build(
            CommissioningView, self._call("POST", "/commissioning/accept", {"seconds": seconds})
        )

    def admit(self, short_id: int) -> CommissioningView:
        return _build(CommissioningView, self._call("POST", f"/commissioning/{short_id:08X}/admit"))

    def factory_reset(self, short_id: int) -> ActionView:
        return _build(
            ActionView, self._call("POST", f"/commissioning/{short_id:08X}/factory-reset")
        )

    # --- configuration -----------------------------------------------------------

    def config(self) -> ConfigView:
        return _build(ConfigView, self._call("GET", "/config"))

    def config_set(self, path: str, value: Any, *, etag: str) -> ConfigView:
        return _build(
            ConfigView, self._call("PUT", f"/config/{path.strip('/')}", {"value": value}, etag=etag)
        )

    def reconcile(self, ref: str) -> ReconcileView:
        return _build(ReconcileView, self._call("POST", f"/nodes/{_quote(ref)}/reconcile"))

    def config_set_attribute(
        self, ref: str, endpoint: int, attribute: str, value: Any, *, etag: str
    ) -> ConfigView:
        return _build(
            ConfigView,
            self._call(
                "PUT",
                f"/config-attribute/{_quote(ref)}/ep/{endpoint}/{_quote(attribute)}",
                {"value": value},
                etag=etag,
            ),
        )

    def config_adopt(self, ref: str, endpoint: int, attribute: str, *, etag: str) -> ConfigView:
        return _build(
            ConfigView,
            self._call(
                "PUT", f"/adopt/{_quote(ref)}/ep/{endpoint}/{_quote(attribute)}", None, etag=etag
            ),
        )

    def config_delete(self, path: str, *, etag: str) -> ConfigView:
        return _build(ConfigView, self._call("DELETE", f"/config/{path.strip('/')}", etag=etag))

    # --- the event stream ---------------------------------------------------------

    @contextmanager
    def stream(self) -> Iterator[Iterator[EventView]]:
        """Live events, until the `with` block is left."""
        with self._sse("/events") as response:
            yield _stream(response, EventView)

    @contextmanager
    def frames(self) -> Iterator[Iterator[TrafficView]]:
        """Live traffic on the bus. A stream separate from `stream()` — a different
        question."""
        with self._sse("/frames") as response:
            yield _stream(response, TrafficView)

    @contextmanager
    def _sse(self, path: str) -> Iterator[HTTPResponse]:
        """A broken connection comes out of here as an `ApiError`, like every other failure —
        the view is to handle one thing, not two."""
        task = self._request("GET", path)
        try:
            response = urllib.request.urlopen(task, timeout=None)
        except urllib.error.HTTPError as exc:
            raise _problem(exc) from None
        except urllib.error.URLError as exc:
            raise ApiError(
                Problem.UNAVAILABLE, f"the hub at {self.url} does not answer: {exc.reason}"
            ) from None
        try:
            yield response
        finally:
            response.close()

    # --- transport ---------------------------------------------------------------

    def _request(
        self, method: str, path: str, body: Any = None, etag: str | None = None
    ) -> urllib.request.Request:
        headers = {"Authorization": f"Bearer {self.token}"}
        if etag is not None:
            headers["If-Match"] = f'"{etag}"'
        content: bytes | None = None
        if body is not None:
            content = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(self.url + path, data=content, headers=headers, method=method)

    def _call(self, method: str, path: str, body: Any = None, etag: str | None = None) -> Any:
        """A hub error comes back as an `ApiError` — the same type as on the server side.

        That way `commands.py` handles one thing rather than two: its own exceptions
        and someone else's HTTP codes.
        """
        task = self._request(method, path, body, etag)
        try:
            with urllib.request.urlopen(task, timeout=self.timeout) as response:
                raw_bytes = response.read()
                return json.loads(raw_bytes) if raw_bytes else None
        except urllib.error.HTTPError as exc:
            raise _problem(exc) from None
        except urllib.error.URLError as exc:
            raise ApiError(
                Problem.UNAVAILABLE, f"the hub at {self.url} does not answer: {exc.reason}"
            ) from None

    def _many[T](self, type_name: type[T], raw_bytes: Any) -> tuple[T, ...]:
        if not isinstance(raw_bytes, list):
            return ()
        return tuple(_build(type_name, item) for item in raw_bytes)


# --- assembling the views --------------------------------------------------------


def _build[T](type_name: type[T], raw_bytes: Any) -> T:
    """JSON -> the same dataclass the hub exposes.

    It follows the annotations, so adding a field to a view in `api/views.py` needs not a
    single line here — while leaving it out on the hub side surfaces as a `TypeError`
    rather than a missing column in a table.
    """
    if not isinstance(raw_bytes, dict):
        raise ApiError(Problem.INVALID, f"expected an object, got {type(raw_bytes).__name__}")
    hints = get_type_hints(type_name)
    values: dict[str, Any] = {}
    for field in fields(type_name):  # type: ignore[arg-type]
        if field.name not in raw_bytes:
            continue
        values[field.name] = _coerce(hints[field.name], raw_bytes[field.name])
    return type_name(**values)


def _coerce(hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(hint)
    if origin in (tuple, UnionType):
        args = [a for a in get_args(hint) if a is not type(None) and a is not Ellipsis]
        if origin is tuple and args:
            return tuple(_coerce(args[0], item) for item in value)
        for candidate in args:
            if _is_view(candidate) and isinstance(value, dict):
                return _build(candidate, value)
        return value
    if _is_view(hint) and isinstance(value, dict):
        return _build(hint, value)
    return value


def _is_view(hint: Any) -> bool:
    """Whether an annotation points at a view that has to be assembled separately."""
    return isinstance(hint, type) and is_dataclass(hint)


def _problem(exc: urllib.error.HTTPError) -> ApiError:
    try:
        content = json.loads(exc.read())
    except (ValueError, OSError):
        content = {}
    if not isinstance(content, dict):
        content = {}
    name = str(content.get("problem", ""))
    return ApiError(
        PROBLEMS.get(name, Problem.INVALID),
        str(content.get("message") or f"the hub answered {exc.code}"),
        result=content.get("result"),
    )


def _stream[T](response: HTTPResponse, type_name: type[T]) -> Iterator[T]:
    """Parsing SSE. A comment (`: ping`) is a heartbeat and not a message."""
    for raw_line in response:
        line = raw_line.decode("utf-8").rstrip("\n")
        if line.startswith("data: "):
            yield _build(type_name, json.loads(line[6:]))


def _quote(text: str) -> str:
    return quote(text, safe="")


# --- where the address and token come from ---------------------------------------


def discover(config: Path, url: str | None = None, token: str | None = None) -> HubClient:
    """Address and token: from the arguments, from the environment, finally from the hub’s
    files.

    The last route is what makes `lagun-hub tui` on the same machine work **without a
    single argument** — and that is the commonest case.
    The directory comes from `--config` or from `$LAGUN_CONFIG`.
    """
    import os

    from lagunhub.adapters.http.auth import TOKEN_FILE

    if url is None:
        url = os.environ.get("LAGUN_URL") or _url_from_config(config)
    if token is None:
        token = os.environ.get(TOKEN_ENV) or _token_from_file(config / TOKEN_FILE)
    if not token:
        raise ApiError(
            Problem.INVALID,
            f"no token: pass --token, set {TOKEN_ENV}, or point --config at a directory with "
            f"{TOKEN_FILE}",
        )
    return HubClient(url, token)


def _url_from_config(config: Path) -> str:
    from lagunhub.core.config import ConfigError, load

    try:
        api = load(config).api
    except (ConfigError, OSError):
        return "http://127.0.0.1:8080"
    host = "127.0.0.1" if api.host in ("0.0.0.0", "::") else api.host
    return f"http://{host}:{api.port}"


def _token_from_file(file_: Path) -> str:
    try:
        return file_.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


__all__ = ["TOKEN_ENV", "HubClient", "discover"]
