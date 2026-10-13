"""Reading everything a node can tell — on an operator's request (zadania/zrobione/FEAT-06.md).

The hub knows a value only when the node reported it, reconciling read it, or somebody
read it by hand, one attribute at a time. This reads **every readable attribute** of a
node, or of one endpoint, and lets each reply land in the model the way every reply
does: through `CoreThread._remembering`. Nothing here stores a value.

It is not polling. The hub does not query nodes by itself after a restart — it listens
for reports — and this is no exception to that: it runs only when a person asks, like
a single read does. A whole installation at once is deliberately not offered (R6).

**One read after another per node**, like reconciling: a relay with four channels is
about 170 reads, and sending them all at once would fill the bus and the node's queue
for nothing. Several nodes may be read at the same time (R4).

Long values (`string`, `blob`) go to the end of the queue: they travel in segments, and
the short ones should appear in the view first (R2).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from lagunhub.core.model import Installation, NodeModel
from lagunhub.core.transactions import Outcome
from lagunhub.protocol.endpoints.definition import AttributeDef
from lagunhub.protocol.registry import MessageKind, ResultCode

Request = Callable[[int, int, int, int, MessageKind, bytes, Callable[[Outcome], None]], None]
"""``(bus, address, endpoint, element, kind, arguments, done)`` — as for reconciling."""

LONG_TYPES = frozenset({"string", "blob"})
"""Types that do not fit in a frame and come back in segments (05-mapowanie-can.md §6)."""


@dataclass(frozen=True, slots=True)
class Summary:
    """How a readout ended — what the closing event and the issue entry say."""

    total: int
    read: int = 0
    absent: int = 0
    """Optional attributes the node turned down. Not a fault: the device may leave them
    out, and CORE has remembered them in `absent`."""

    refused: tuple[str, ...] = ()
    """``ep4 Enabled`` — **mandatory** attributes the node turned down with
    `UNSUPPORTED_ELEMENT`. The node failing its type (R8)."""

    failed: tuple[str, ...] = ()
    """``ep0 VendorName → BUSY`` — any other refusal."""

    silent: str | None = None
    """The attribute the node fell silent on. The readout stops there."""

    def text(self) -> str:
        if self.silent is not None:
            return (
                f"stopped at {self.silent}: the node does not answer "
                f"({self.read} of {self.total} read)"
            )
        parts = [f"{self.read} read"]
        if self.absent:
            parts.append(f"{self.absent} absent")
        if self.refused:
            parts.append(f"{len(self.refused)} refused")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts)


@dataclass
class Task:
    """One readout in progress."""

    node: NodeModel
    pending: list[tuple[int, AttributeDef]]
    total: int = 0
    read: int = 0
    absent: int = 0
    refused: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def summary(self, silent: str | None = None) -> Summary:
        return Summary(
            total=self.total,
            read=self.read,
            absent=self.absent,
            refused=tuple(self.refused),
            failed=tuple(self.failed),
            silent=silent,
        )


class Readout:
    """Reads every readable attribute of a node. Belongs to CORE."""

    def __init__(
        self,
        installation: Installation,
        request: Request,
        on_done: Callable[[NodeModel, Summary], None] | None = None,
        on_issue: Callable[[str], None] | None = None,
    ) -> None:
        self.installation = installation
        self._request = request
        self._on_done = on_done
        self._on_issue = on_issue or installation.issues.append
        self._tasks: dict[bytes, Task] = {}

    def __len__(self) -> int:
        return len(self._tasks)

    def busy(self, uid: bytes) -> bool:
        return bytes(uid) in self._tasks

    def remaining(self, uid: bytes) -> int | None:
        """How many reads are still ahead — `None` when this node is not being read."""
        task = self._tasks.get(bytes(uid))
        return len(task.pending) if task is not None else None

    def start(self, node: NodeModel, endpoint: int | None = None) -> int:
        """Queue the reads and send the first. Returns how many there are — `0` when there
        is nothing to read, or this node is already being read or is blocked.

        The caller (the API) says *why* it refuses; here a refusal is only a zero, as with
        `Reconciler.start`.
        """
        key = bytes(node.uid)
        if key in self._tasks or self.installation.is_blocked(node.uid):
            return 0
        pending = self._wanted(node, endpoint)
        if not pending:
            return 0
        task = Task(node=node, pending=pending, total=len(pending))
        self._tasks[key] = task
        self._step(task)
        return task.total

    def cancel(self, uid: bytes) -> bool:
        """Stop reading this node — it was removed, blocked, or is being enumerated again
        (R5). Enumeration builds a new model, so reading the old one would only fill
        something about to be thrown away.

        Replies already on their way still arrive and still reach the model through CORE;
        `_live` only stops them from sending the next read.
        """
        return self._tasks.pop(bytes(uid), None) is not None

    def _live(self, task: Task) -> bool:
        return self._tasks.get(bytes(task.node.uid)) is task

    # --- what to read ---------------------------------------------------------

    @staticmethod
    def _wanted(node: NodeModel, only: int | None) -> list[tuple[int, AttributeDef]]:
        """Every readable attribute the endpoint has, short values first.

        Left out without asking: what needs a feature the endpoint did not declare, the
        optional attributes the node already turned down (both from `lacks`), and
        endpoints whose type the hub does not know — there is no list of attributes to
        go by.
        """
        short: list[tuple[int, AttributeDef]] = []
        long: list[tuple[int, AttributeDef]] = []
        for number in sorted(node.endpoints):
            if only is not None and number != only:
                continue
            model = node.endpoints[number]
            definition = model.definition
            if definition is None:
                continue
            for element in sorted(definition.attributes):
                meta = definition.attributes[element]
                if not meta.readable or model.lacks(meta) is not None:
                    continue
                (long if meta.type in LONG_TYPES else short).append((number, meta))
        return short + long

    # --- step by step ---------------------------------------------------------

    def _step(self, task: Task) -> None:
        if not task.pending:
            del self._tasks[bytes(task.node.uid)]
            self._finish(task, task.summary())
            return
        endpoint, meta = task.pending[0]
        self._request(
            task.node.bus,
            task.node.address,
            endpoint,
            meta.id,
            MessageKind.READ,
            b"",
            lambda outcome: self._got(task, endpoint, meta, outcome),
        )

    def _got(self, task: Task, endpoint: int, meta: AttributeDef, outcome: Outcome) -> None:
        if not self._live(task):
            return
        where = f"ep{endpoint} {meta.name}"
        if not outcome.answered:
            # One silence ends it, as in reconciling: three attempts already went out,
            # and a node that is gone would otherwise cost 300 ms for each of 170 reads.
            del self._tasks[bytes(task.node.uid)]
            self._finish(task, task.summary(silent=where))
            return
        task.pending.pop(0)
        if outcome.result == ResultCode.OK:
            task.read += 1
        elif outcome.result == ResultCode.UNSUPPORTED_ELEMENT and meta.optional:
            task.absent += 1  # CORE has put it in `absent` (R9)
        elif outcome.result == ResultCode.UNSUPPORTED_ELEMENT:
            task.refused.append(where)
        else:
            name = outcome.result.name if outcome.result is not None else "?"
            task.failed.append(f"{where} → {name}")
        self._step(task)

    def _finish(self, task: Task, summary: Summary) -> None:
        """One issue entry per readout, not one per attribute (R8).

        A node on a library from before zadania/zrobione/TASK-01.md lacks several common
        attributes and refuses a dozen of them; an entry each would bury the list.
        """
        node = task.node
        name = node.name or node.uid.hex()
        who = f"{name} (0x{node.address:03X})"
        if summary.silent is not None:
            self._on_issue(f"{who}: reading everything {summary.text()}")
        else:
            details: list[str] = []
            if summary.refused:
                details.append(
                    f"refuses {len(summary.refused)} mandatory attribute(s): "
                    + ", ".join(summary.refused)
                )
            if summary.failed:
                details.append("failed: " + ", ".join(summary.failed))
            if details:
                self._on_issue(f"{who}: reading everything — " + "; ".join(details))
        if self._on_done is not None:
            self._on_done(node, summary)
