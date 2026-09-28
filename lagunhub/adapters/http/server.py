"""The hub server — REST for reading and control, SSE for the event stream.

The resolution of point **H7**: **the standard library**, `ThreadingHTTPServer`.

We considered FastAPI (OpenAPI for free) and Starlette without pydantic (lighter). Both
lost to one argument: **the hub is threaded**. A `HubApi` operation blocks on a
`Future`, and that is its design rather than an oversight — it stalls one request, not
the hub ([`ARCHITEKTURA.md`](../../../ARCHITEKTURA.md) §10.7). An `asyncio` server would
have to send every such call off to a thread pool so as not to block the event loop —
that is, go through the whole `async` ceremony to land on the thread we started from.
`ThreadingHTTPServer` gives a thread per request directly and fits the rest without a
single adapter piece.

There is a second thing: the core has **one** dependency (`ruamel.yaml`) and is happy
with that on an RPi. The server is a translator of some three hundred lines — that is
not where this project’s value lives.

What this choice costs, honestly: the documentation itself advises against `http.server`
for public use, and rightly — there are no request limits and no hard defence against a
slow client. For a hub on the loopback or on a home network, behind a token, that is an
acceptable price. Were it ever to stop being one, **the replacement touches one file**,
because everything that matters sits in `lagunhub.api`. That is what this boundary is for.
"""

from __future__ import annotations

import json
import re
import select
import socket
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, is_dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, unquote, urlparse

from lagunhub import __version__
from lagunhub.adapters.http import auth
from lagunhub.api import ApiError, HubApi, Problem, Stream

HEARTBEAT: Final = 15.0
"""How often to send a comment on the SSE stream. Without it a broken connection looks
identical to silence in the installation — and those are two entirely different
things."""

MAX_BODY: Final = 64 * 1024

_STREAMS: Final[dict[str, Callable[[HubApi], AbstractContextManager[Stream[Any, Any]]]]] = {
    "/events": lambda api: api.stream(),
    "/frames": lambda api: api.frames(),
}
"""Two streams, because they are two different questions: `events` says **what
happened**, `frames` **what travelled the wire**. The first is enough to draw the state;
the second is needed to understand why the state is what it is
([`core/traffic.py`](../../core/traffic.py))."""

POLL: Final = 0.02
"""How often the listening loop checks whether it should stop. The standard library’s
default half a second is visible here outright: that is how long every hub shutdown
would take."""

STATUS: Final[dict[Problem, HTTPStatus]] = {
    Problem.NOT_FOUND: HTTPStatus.NOT_FOUND,
    Problem.INVALID: HTTPStatus.BAD_REQUEST,
    Problem.UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    Problem.REFUSED: HTTPStatus.CONFLICT,
    Problem.SILENT: HTTPStatus.GATEWAY_TIMEOUT,
    Problem.TIMEOUT: HTTPStatus.INTERNAL_SERVER_ERROR,
    Problem.CONFLICT: HTTPStatus.CONFLICT,
    Problem.REJECTED: HTTPStatus.UNPROCESSABLE_ENTITY,
}
"""`SILENT` is 504 and `TIMEOUT` is 500 — and that difference is deliberate: the first
means "the node did not answer", the second "the **hub** did not answer". The second is
our fault."""


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    query: Mapping[str, str]
    body: Any
    headers: Mapping[str, str]
    args: tuple[str, ...]
    """Groups pulled out of the path, already decoded from `%xx`."""

    def number(self, index: int) -> int:
        try:
            return int(self.args[index])
        except ValueError:
            raise ApiError(Problem.INVALID, f"{self.args[index]!r} is not a number") from None

    def if_match(self) -> str:
        """The `ETag` the client saw. Without it a write is blind and must not be done.

        There is no "overwrite anyway" mode: whoever wants to overwrite does a `GET`
        first and **sees** what they overwrite
        ([`KONFIGURACJA.md`](../../../KONFIGURACJA.md) §11.2).
        """
        given = self.headers.get("If-Match", "").strip().strip('"')
        if not given:
            raise ApiError(Problem.INVALID, "a configuration write requires an If-Match header")
        return given

    def field(self, name: str, default: Any = None) -> Any:
        if self.body is None:
            return default
        if not isinstance(self.body, dict):
            raise ApiError(Problem.INVALID, "the request body is to be a JSON object")
        return self.body.get(name, default)


@dataclass(frozen=True, slots=True)
class Response:
    status: HTTPStatus = HTTPStatus.OK
    body: Any = None
    etag: str = ""


Handler = Callable[[Request], Response]
Route = tuple[str, re.Pattern[str], Handler]


def routes(api: HubApi) -> list[Route]:
    """The route table. Each is **thin**: it unpacks the path and calls `HubApi`.

    What is not here, deliberately ([`ARCHITEKTURA.md`](../../../ARCHITEKTURA.md) §10.6):
    addressing a node by address (an address is a route, not an identity) and choosing
    a bus (the hub knows by itself where it sees whom).
    """

    def _config(request: Request) -> Response:
        del request
        view = api.config()
        return Response(body=view, etag=view.etag)

    def _set(request: Request) -> Response:
        view = api.config_set(request.args[0], request.field("value"), etag=request.if_match())
        return Response(body=view, etag=view.etag)

    def _set_attribute(request: Request) -> Response:
        view = api.config_set_attribute(
            request.args[0],
            request.number(1),
            request.args[2],
            request.field("value"),
            etag=request.if_match(),
        )
        return Response(body=view, etag=view.etag)

    def _adopt(request: Request) -> Response:
        view = api.config_adopt(
            request.args[0], request.number(1), request.args[2], etag=request.if_match()
        )
        return Response(body=view, etag=view.etag)

    def _delete(request: Request) -> Response:
        view = api.config_delete(request.args[0], etag=request.if_match())
        return Response(body=view, etag=view.etag)

    def info(request: Request) -> Response:
        del request
        return Response(body=api.info())

    return [
        ("GET", re.compile(r"^/$"), info),
        ("GET", re.compile(r"^/nodes$"), lambda r: Response(body=api.nodes())),
        ("GET", re.compile(r"^/nodes/([^/]+)$"), lambda r: Response(body=api.node(r.args[0]))),
        (
            "GET",
            re.compile(r"^/nodes/([^/]+)/ep/(\d+)$"),
            lambda r: Response(body=api.endpoint(r.args[0], r.number(1))),
        ),
        (
            "POST",
            re.compile(r"^/nodes/([^/]+)/ep/(\d+)/commands/([^/]+)$"),
            lambda r: Response(
                body=api.command(r.args[0], r.number(1), r.args[2], r.field("args") or {})
            ),
        ),
        (
            "GET",
            re.compile(r"^/nodes/([^/]+)/ep/(\d+)/attributes/([^/]+)$"),
            lambda r: Response(body=api.read(r.args[0], r.number(1), r.args[2])),
        ),
        (
            "PUT",
            re.compile(r"^/nodes/([^/]+)/ep/(\d+)/attributes/([^/]+)$"),
            lambda r: Response(body=api.write(r.args[0], r.number(1), r.args[2], r.field("value"))),
        ),
        (
            "POST",
            re.compile(r"^/nodes/([^/]+)/identify$"),
            lambda r: Response(body=api.identify(r.args[0], int(r.field("seconds", 5)))),
        ),
        ("GET", re.compile(r"^/config$"), _config),
        ("PUT", re.compile(r"^/config/(.+)$"), _set),
        ("DELETE", re.compile(r"^/config/(.+)$"), _delete),
        ("GET", re.compile(r"^/diff$"), lambda r: Response(body=api.diff())),
        (
            "POST",
            re.compile(r"^/nodes/([^/]+)/reconcile$"),
            lambda r: Response(body=api.reconcile(r.args[0])),
        ),
        ("PUT", re.compile(r"^/adopt/([^/]+)/ep/(\d+)/([^/]+)$"), _adopt),
        ("PUT", re.compile(r"^/config-attribute/([^/]+)/ep/(\d+)/([^/]+)$"), _set_attribute),
        ("GET", re.compile(r"^/bindings$"), lambda r: Response(body=api.bindings())),
        ("GET", re.compile(r"^/issues$"), lambda r: Response(body=api.issues())),
        ("GET", re.compile(r"^/health$"), lambda r: Response(body=api.health())),
        ("GET", re.compile(r"^/commissioning$"), lambda r: Response(body=api.commissioning())),
        (
            "POST",
            re.compile(r"^/commissioning/accept$"),
            lambda r: Response(body=api.accept_new(float(r.field("seconds", 900)))),
        ),
        (
            "POST",
            re.compile(r"^/commissioning/([0-9A-Fa-fx]+)/admit$"),
            lambda r: Response(body=api.admit(_short_id(r.args[0]))),
        ),
        (
            "POST",
            re.compile(r"^/commissioning/([0-9A-Fa-fx]+)/factory-reset$"),
            lambda r: Response(body=api.factory_reset(_short_id(r.args[0]))),
        ),
    ]


class HubServer:
    """The hub server. It starts a thread of its own and touches no other."""

    def __init__(
        self,
        api: HubApi,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        token: str = "",
        config_dir: Path | None = None,
    ) -> None:
        self.api = api
        self.token, self.token_is_new = auth.resolve(token, config_dir)
        self.routes = routes(api)
        self.stopping = threading.Event()
        self.requests = 0
        self.rejected = 0
        self.streams = 0
        self.disconnects = 0
        """Clients that left without saying goodbye. Normal, but worth knowing how many."""

        self.errors = 0

        handler = _make_handler(self)
        self._http = _Listener((host, port), handler, self)
        self._http.daemon_threads = True
        self._thread = threading.Thread(
            target=lambda: self._http.serve_forever(poll_interval=POLL),
            name="api",
            daemon=True,
        )

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._http.server_address[:2]
        return str(host), int(port)

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """First we wake the streams, then we close the listener — in that order, because
        `shutdown()` waits for the threads and an SSE stream waits forever."""
        self.stopping.set()
        self._http.shutdown()
        self._http.server_close()
        self._thread.join(timeout=2.0)


class _Listener(ThreadingHTTPServer):
    """A `ThreadingHTTPServer` that does not shout when a client walks away.

    By default a broken connection prints a full stack trace to the console — and a
    broken connection is a **normal ending** here: that is what a closed browser tab
    looks like and what leaving the shell looks like. A hub that prints twenty lines on
    every such event teaches whoever is watching to ignore its log.
    """

    def __init__(self, address: tuple[str, int], handler: Any, owner: HubServer) -> None:
        super().__init__(address, handler)
        self.owner = owner

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys
        import traceback

        type_name = sys.exc_info()[0]
        if type_name is not None and issubclass(type_name, OSError):
            self.owner.disconnects += 1
            return
        self.owner.errors += 1
        print(f"!! request handling error: {traceback.format_exc(limit=1).strip()}", flush=True)


# --- handling a single request ---------------------------------------------------


def _make_handler(server: HubServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"lagun-hub/{__version__}"

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PUT(self) -> None:
            self._handle("PUT")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

        def log_message(self, format: str, *args: Any) -> None:
            """Silence. The hub log is a separate matter (ARCHITEKTURA.md §14.4)."""

        # --- traffic ---------------------------------------------------------

        def _handle(self, method: str) -> None:
            server.requests += 1
            breakdown = urlparse(self.path)
            path = unquote(breakdown.path)
            query = {k: v[0] for k, v in parse_qs(breakdown.query).items()}

            if not self._authorised(query):
                server.rejected += 1
                self._send(HTTPStatus.UNAUTHORIZED, {"problem": "unauthorized"})
                return

            if method == "GET" and path in _STREAMS:
                self._stream(path)
                return

            try:
                body = self._body()
                response = self._route(method, path, query, body)
            except ApiError as exc:
                self._send(
                    STATUS.get(exc.problem, HTTPStatus.INTERNAL_SERVER_ERROR),
                    {"problem": exc.problem.value, "message": exc.message, "result": exc.result},
                )
                return
            self._send(response.status, response.body, etag=response.etag)

        def _authorised(self, query: Mapping[str, str]) -> bool:
            """A header or a query parameter. The parameter is needed because the browser’s
            `EventSource` cannot set a header — and for that reason only."""
            header = self.headers.get("Authorization", "")
            given = header[7:] if header.startswith("Bearer ") else query.get("token")
            return auth.matches(server.token, given)

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return None
            if length > MAX_BODY:
                raise ApiError(Problem.INVALID, f"request body larger than {MAX_BODY} B")
            raw_bytes = self.rfile.read(length)
            try:
                return json.loads(raw_bytes)
            except json.JSONDecodeError as exc:
                raise ApiError(Problem.INVALID, f"the body is not valid JSON: {exc}") from None

        def _route(self, method: str, path: str, query: Mapping[str, str], body: Any) -> Response:
            known_path = False
            for route_method, pattern, handle in server.routes:
                match = pattern.match(path)
                if match is None:
                    continue
                known_path = True
                if route_method != method:
                    continue
                return handle(
                    Request(
                        method=method,
                        path=path,
                        query=query,
                        body=body,
                        headers=dict(self.headers.items()),
                        args=match.groups(),
                    )
                )
            if known_path:
                # 405, not 404: the path exists, just not by this method. The
                # difference is so that a typo in the method does not look like a typo
                # in the address.
                return Response(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"problem": "method_not_allowed", "message": f"{method} not on {path}"},
                )
            raise ApiError(Problem.NOT_FOUND, f"there is no path {path}")

        # --- the event stream -------------------------------------------------

        def _stream(self, path: str) -> None:
            # The response has no length and never ends, so no further request can be
            # read after it. Without this `BaseHTTPRequestHandler` tries anyway and
            # falls over on a closed socket.
            self.close_connection = True
            server.streams += 1
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                open_bus = _STREAMS[path]
                with open_bus(server.api) as stream:
                    self._pump(stream)
            except OSError:
                # The client walked away — a broken connection is not a hub fault. We
                # catch it together with the headers, because one can leave between them
                # too.
                pass
            finally:
                server.streams -= 1

        def _pump(self, events: Stream[Any, Any]) -> None:
            """Silence is an event too — a heartbeat has to go out in it.

            That is why we read with a timeout rather than in a `for` loop: otherwise
            through an hour of quiet not a single byte would go out and the client would
            have no way of telling a broken connection from an installation where
            nothing is happening.
            """
            self._write(": connected\n\n")
            recent = time.monotonic()
            while not server.stopping.is_set() and not events.closed and not self._gone():
                event = events.get(timeout=0.5)
                if event is not None:
                    self._write(f"event: {event.kind}\ndata: {_dumps(event)}\n\n")
                    recent = time.monotonic()
                elif time.monotonic() - recent >= HEARTBEAT:
                    self._write(": ping\n\n")
                    recent = time.monotonic()

        def _gone(self) -> bool:
            """Whether the client has already walked away.

            Without this we only learn of it at the next write — and in a quiet
            installation that means at the heartbeat, that is, after a dozen-odd
            seconds. All that time a dead client holds a thread and a subscription, and
            with a front end that likes to reconnect, several such corpses pile up at
            once.

            A socket closed by the other side is **ready to read and empty** — and that
            is the whole trick.
            """
            try:
                done, _, _ = select.select([self.connection], [], [], 0)
                if not done:
                    return False
                suspect: bytes = self.connection.recv(1, socket.MSG_PEEK)
                return suspect == b""
            except OSError:
                return True

        def _write(self, text: str) -> None:
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()

        # --- the response -----------------------------------------------------

        def _send(self, status: HTTPStatus, body: Any, etag: str = "") -> None:
            raw_bytes = _dumps(body).encode("utf-8") if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if etag:
                self.send_header("ETag", f'"{etag}"')
            self.send_header("Content-Length", str(len(raw_bytes)))
            self.end_headers()
            if raw_bytes:
                self.wfile.write(raw_bytes)

    return Handler


def _dumps(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False)


def _plain(value: Any) -> Any:
    """The views are tuples of frozen dataclasses — JSON does not know about them, we
    do."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in asdict(value).items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def free_port() -> int:
    """A free port — for tests, so as not to queue up for 8080."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


__all__ = ["HEARTBEAT", "HubServer", "Request", "Response", "free_port", "routes"]


def _short_id(text: str) -> int:
    """A `ShortId` in a path — hexadecimal, as the commissioning view prints it."""
    try:
        return int(text, 16)
    except ValueError:
        raise ApiError(Problem.INVALID, f"{text!r} is not a ShortId") from None
