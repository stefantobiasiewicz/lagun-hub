"""What is on the screen and where it comes from.

Separated from drawing, because these are two different things that break for different
reasons: here it is about **freshness and backpressure**, there about characters in a
terminal.

Two rules, both from [`ARCHITEKTURA.md`](../../../ARCHITEKTURA.md) §10.3.3:

* **no call to the hub happens on the drawing thread** — not even the shortest, because
  a terminal that freezes for the length of a reply is worse than a slow one,
* **backpressure belongs to the view** — the feed has a ring buffer of its own that
  drops the oldest and **says how many**; it must never be allowed to slow down what
  it reads from.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Final

from lagunhub.adapters.client.client import Hub
from lagunhub.api import (
    ApiError,
    BindingView,
    CommissioningView,
    DiffRow,
    EndpointView,
    HealthView,
    HubInfo,
    NodeSummary,
    NodeView,
    Problem,
    TrafficView,
)

FEED_DEPTH: Final = 2000
"""How many frames the feed keeps. At five hundred per second that is four seconds of
history — as much as a person can read before scrolling anyway."""

REFRESH: Final = 2.0
"""How often the lists are fetched again. The view reloads at once after its own actions,
but a change that comes from the installation — a node joining, a click, a value
reported — reaches the lists **only by this poll**, so it shows up to two seconds late.
The feed below is no help: it carries `frames()`, and nothing reloads on a frame.
Reloading on `stream()` events would make this a safety net instead of the main route
([`ARCHITEKTURA.md`](../../../ARCHITEKTURA.md) §14)."""

RETRY: Final = 2.0


class Section(Enum):
    NODES = "Nodes"
    BINDINGS = "Bindings"
    DIFF = "Drift"
    ISSUES = "Issues"
    JOINING = "Joining"
    HEALTH = "Health"


class Focus(Enum):
    """Where the cursor is.

    Three levels, one under the other: sections -> list -> details. **None disappears**
    when one enters the next — only what is highlighted changes. `→` and `⏎` go down,
    `←` and `⌫` come back.
    """

    NAV = "sections"
    LIST = "list"
    DETAIL = "details"


@dataclass
class Feed:
    """The feed’s ring buffer. The stream thread writes, the drawing thread reads."""

    depth: int = FEED_DEPTH
    items: list[TrafficView] = field(default_factory=list)
    dropped: int = 0
    paused: bool = False
    connected: bool = False
    rate: float = 0.0
    _window: list[float] = field(default_factory=list)

    def add(self, item: TrafficView) -> None:
        now = time.monotonic()
        self._window.append(now)
        if len(self._window) > 128:
            del self._window[:64]
        self.rate = float(sum(1 for t in self._window if now - t <= 1.0))
        if self.paused:
            return
        self.items.append(item)
        if len(self.items) > self.depth:
            # We drop the OLDEST and count. Stalling here would mean the slowest view
            # slows the hub down — and that is not allowed.
            excess = len(self.items) - self.depth
            del self.items[:excess]
            self.dropped += excess


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything the drawer has to hand. Substituted wholesale, as in the hub."""

    info: HubInfo | None = None
    nodes: tuple[NodeSummary, ...] = ()
    bindings: tuple[BindingView, ...] = ()
    diff: tuple[DiffRow, ...] = ()
    issues: tuple[str, ...] = ()
    joining: CommissioningView | None = None
    health: HealthView | None = None
    node: NodeView | None = None
    """An expanded node — fetched separately, because nobody needs all of them at once."""

    etag: str = ""
    error: str = ""
    at: float = 0.0


class Worker(threading.Thread):
    """One thread for every conversation with the hub.

    One, not a pool: requests go in order, so `t` pressed three times in a row gives
    three commands in that same order. A pool would give a race over which arrives
    first — with a light being controlled, that is visible to the naked eye.
    """

    def __init__(self, on_done: Callable[[str], None]) -> None:
        super().__init__(name="tui-worker", daemon=True)
        self._queue: queue.Queue[Callable[[], str] | None] = queue.Queue()
        self._on_done = on_done

    def submit(self, task: Callable[[], str]) -> None:
        self._queue.put(task)

    def stop(self) -> None:
        self._queue.put(None)

    def run(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                return
            try:
                self._on_done(task())
            except ApiError as exc:
                self._on_done(f"! {exc.message}")
            except OSError as exc:
                self._on_done(f"! {exc}")


class State:
    """The view’s state plus the threads that feed it."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.snapshot = Snapshot()
        self.feed = Feed()
        self.section = Section.NODES
        self.focus = Focus.LIST
        self.cursor: dict[Section, int] = dict.fromkeys(Section, 0)
        self.detail_cursor = 0
        self.endpoint: int | None = None
        """The endpoint expanded in the details panel. `None` means the endpoint list."""

        self.filter = ""
        """The list filter. Empty means "everything" — and that is what `Esc` leaves."""

        self.status = ""
        self.overlay: Any = None
        """The window on top — an edit or a confirmation. `None` means none."""

        self.dirty = True
        self.stopping = threading.Event()
        self.worker = Worker(self.say)
        self._threads = [
            threading.Thread(target=self._refresh_loop, name="tui-refresh", daemon=True),
            threading.Thread(target=self._feed_loop, name="tui-feed", daemon=True),
        ]

    # --- life cycle ---------------------------------------------------------------

    def start(self) -> None:
        self.worker.start()
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.worker.stop()

    def say(self, text: str) -> None:
        self.status = text
        self.dirty = True

    # --- the feeding threads ------------------------------------------------------

    def _refresh_loop(self) -> None:
        while not self.stopping.is_set():
            self.reload()
            self.stopping.wait(REFRESH)

    def _feed_loop(self) -> None:
        """Traffic from the hub. A broken connection **does not end the view** — it keeps
        trying."""
        while not self.stopping.is_set():
            try:
                with self.hub.frames() as frames:
                    self.feed.connected = True
                    for item in frames:
                        if self.stopping.is_set():
                            return
                        self.feed.add(item)
                        self.dirty = True
            except (ApiError, OSError):
                pass
            self.feed.connected = False
            self.dirty = True
            self.stopping.wait(RETRY)

    def reload(self) -> None:
        """Fetch everything visible. Called from the refresh thread."""
        ref = self.node_ref()
        try:
            fresh = Snapshot(
                info=self.hub.info(),
                nodes=self.hub.nodes(),
                bindings=self.hub.bindings(),
                diff=self.hub.diff(),
                issues=self.hub.issues(),
                joining=self.hub.commissioning(),
                health=self.hub.health(),
                node=self._node(ref),
                etag=self.hub.config().etag,
                at=time.monotonic(),
            )
        except (ApiError, OSError) as exc:
            reason = exc.message if isinstance(exc, ApiError) else str(exc)
            self.snapshot = replace(self.snapshot, error=reason)
            self.dirty = True
            return
        self.snapshot = fresh  # a wholesale swap — the drawer never sees half of one
        self.dirty = True

    def _node(self, ref: str | None) -> NodeView | None:
        """The node under the cursor — which may be gone by now.

        `ref` comes from the list as it was **before** this refresh. After a removal it
        names the node just forgotten, and `NOT_FOUND` is then the true answer, not a
        failure: treating it as one kept the whole snapshot — the removed node included —
        and every later refresh failed on the same name (zadania/zrobione/FEAT-04.md).
        """
        if not ref:
            return None
        try:
            return self.hub.node(ref)
        except ApiError as exc:
            if exc.problem is Problem.NOT_FOUND:
                return None
            raise

    # --- what is selected --------------------------------------------------------

    def node_ref(self) -> str | None:
        """The node the details panel is about — the one under the list cursor."""
        if self.section is not Section.NODES:
            return None
        selected = self.selected()
        if isinstance(selected, NodeSummary):
            return selected.alias or selected.uid
        return None

    def list_rows(self) -> list[Any]:
        """What is **visible** in the list — not what the hub sent.

        The pane can filter rows out; counting positions independently would make the
        cursor point at something other than the highlight ([`panes.py`](panes.py)).
        """
        from lagunhub.adapters.tui import panes

        return list(panes.items(self).items)

    def selected(self) -> Any:
        """What is highlighted in the list — **exactly what is visible**."""
        return _under_cursor(self.list_rows(), self.cursor[self.section])

    def detail_rows(self) -> list[Any]:
        """The details panel’s items — **the same ones that are visible**, in that order."""
        from lagunhub.adapters.tui import panes

        return list(panes.details(self).items)

    def detail_selected(self) -> Any:
        """The same for the details panel. One source of truth about what is selected."""
        return _under_cursor(self.detail_rows(), self.detail_cursor)

    # --- cursor movement ---------------------------------------------------------

    def move(self, delta: int) -> None:
        if self.focus is Focus.NAV:
            sections = list(Section)
            fresh_one = sections[
                max(0, min(len(sections) - 1, sections.index(self.section) + delta))
            ]
            if fresh_one is not self.section:
                self.section = fresh_one
                self.focus = Focus.NAV
                self.endpoint = None
                self.detail_cursor = 0
                self.worker.submit(self._pull_node)
            self.dirty = True
            return
        if self.focus is Focus.DETAIL:
            rows = self.detail_rows()
            if rows:
                self.detail_cursor = max(0, min(len(rows) - 1, self.detail_cursor + delta))
        else:
            rows = self.list_rows()
            if rows:
                # From the row that is highlighted, not from the stored number: a list
                # that got shorter (a node removed, a filter typed) leaves the number past
                # the end, `_under_cursor` shows the last row, and counting from the
                # number made the first `↑` land on that same row — a key that seemed
                # dead.
                latest = min(self.cursor[self.section], len(rows) - 1)
                fresh = max(0, min(len(rows) - 1, latest + delta))
                if fresh != latest:
                    self.cursor[self.section] = fresh
                    self.detail_cursor = 0
                    self.endpoint = None
                    self.worker.submit(self._pull_node)
        self.dirty = True

    def section_by(self, delta: int) -> None:
        sections = list(Section)
        self.section = sections[(sections.index(self.section) + delta) % len(sections)]
        self.focus = Focus.LIST
        self.endpoint = None
        self.detail_cursor = 0
        self.worker.submit(self._pull_node)
        self.dirty = True

    def enter(self) -> None:
        """Go deeper. The previous level **does not disappear** — it only loses its
        highlight."""
        self.dirty = True
        if self.focus is Focus.NAV:
            self.focus = Focus.LIST
            return
        if self.section is not Section.NODES:
            return
        if self.focus is Focus.LIST:
            if self.snapshot.node is not None:
                self.focus = Focus.DETAIL
                self.detail_cursor = 0
            return
        if self.endpoint is None:
            selected = self.detail_selected()
            if isinstance(selected, EndpointView):
                self.endpoint = selected.number
                self.detail_cursor = 0

    def back(self) -> bool:
        """A step back. `False` means "there is nowhere to go" — and **then nothing happens**.

        Quitting is on `q`, not here: a key one navigates with must not close the view
        on one press too many.
        """
        self.dirty = True
        if self.endpoint is not None:
            self.endpoint = None
            self.detail_cursor = 0
        elif self.focus is Focus.DETAIL:
            self.focus = Focus.LIST
        elif self.focus is Focus.LIST:
            self.focus = Focus.NAV
        else:
            return False
        return True

    def _pull_node(self) -> str:
        ref = self.node_ref()
        if ref:
            self.snapshot = replace(self.snapshot, node=self.hub.node(ref))
            self.dirty = True
        return ""

    # --- what can be controlled ---------------------------------------------------

    def output_endpoint(self) -> EndpointView | None:
        """An output that can be driven from the keyboard. `None` means "there is none"."""
        node = self.snapshot.node
        if node is None:
            return None
        if self.endpoint is not None:
            selected = next((e for e in node.detail if e.number == self.endpoint), None)
            return selected if selected and selected.type_name == "Output" else None
        selected = self.detail_selected() if self.focus is Focus.DETAIL else None
        if isinstance(selected, EndpointView) and selected.type_name == "Output":
            return selected
        return next((e for e in node.detail if e.type_name == "Output"), None)


def _under_cursor(rows: list[Any], cursor: int) -> Any:
    if not rows:
        return None
    return rows[min(cursor, len(rows) - 1)]


__all__ = ["FEED_DEPTH", "REFRESH", "Feed", "Focus", "Section", "Snapshot", "State", "Worker"]
