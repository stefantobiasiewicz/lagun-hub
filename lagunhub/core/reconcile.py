"""Reconciling the configuration with the nodes — 07-hub.md §5.3.

> **The hub writes to the node what it holds itself — that way round, never the
> other.**

Reading serves to **check agreement**, not to restore. From nodes a number comes back,
never a meaning: `GetMembership` will say which groups an endpoint belongs to, but not
what group `5` means. A hub that lost its configuration will recover the structure from
the installation and **not a single operator decision**.

That is why drift lands on the issue list rather than in a log and out of mind: better
to show a person a list of things to configure anew than an installation that works in
appearance only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from lagunhub.core.model import Installation, NodeModel
from lagunhub.core.transactions import Outcome
from lagunhub.protocol.endpoints import CATALOG
from lagunhub.protocol.endpoints.definition import AttributeDef
from lagunhub.protocol.registry import MessageKind, ResultCode
from lagunhub.protocol.values import Value, decode, encode

Request = Callable[[int, int, int, int, MessageKind, bytes, Callable[[Outcome], None]], None]
"""``(bus, address, endpoint, element, kind, arguments, done)``."""


@dataclass
class Task:
    """One reconciliation in progress."""

    node: NodeModel
    pending: list[tuple[int, AttributeDef, Value]] = field(default_factory=list)
    written: int = 0
    checked: int = 0


class Reconciler:
    """Brings a node to the state the configuration describes. Belongs to CORE."""

    def __init__(
        self,
        installation: Installation,
        request: Request,
        on_issue: Callable[[str], None] | None = None,
    ) -> None:
        self.installation = installation
        self._request = request
        self._on_issue = on_issue or installation.issues.append
        self._tasks: dict[bytes, Task] = {}

        self.checked = 0
        self.written = 0
        self.mismatched = 0
        self.failed = 0

    def __len__(self) -> int:
        return len(self._tasks)

    def start(self, node: NodeModel) -> bool:
        """Check agreement for every configured attribute of this node."""
        key = bytes(node.uid)
        if key in self._tasks or self.installation.is_blocked(node.uid):
            # A blocked node is not configured (06-cykl-zycia-urzadzenia.md §8).
            return False
        task = Task(node=node, pending=list(self._wanted(node)))
        if not task.pending:
            return False
        self._tasks[key] = task
        self._step(task)
        return True

    def cancel(self, uid: bytes) -> bool:
        """Stop reconciling this node — it was removed or blocked.

        Replies already on their way still arrive; `_live` makes them land nowhere,
        rather than carrying on with writes to a node the hub has let go of.
        """
        return self._tasks.pop(bytes(uid), None) is not None

    def _live(self, task: Task) -> bool:
        return self._tasks.get(bytes(task.node.uid)) is task

    # --- what we want ---------------------------------------------------------

    def _wanted(self, node: NodeModel) -> list[tuple[int, AttributeDef, Value]]:
        wanted = self.installation.wanted.get(node.uid)
        out: list[tuple[int, AttributeDef, Value]] = []
        for number, description in wanted.endpoints.items() if wanted else ():
            present = node.endpoints.get(number)
            if present is None or present.type_id is None:
                if description.attributes:
                    self._issue(
                        node, f"ep{number} is in the configuration and absent from the node"
                    )
                continue
            definition = CATALOG.get(present.type_id)
            if definition is None:
                continue
            declared = description.type
            if declared is not None and declared != present.type_id:
                # Second validation phase: the file says something other than the
                # descriptor.
                self._issue(
                    node,
                    f"ep{number} is of type {present.type_id.name}, "
                    f"while the configuration says {declared.name}",
                )
                continue
            for element, value in description.attributes.items():
                meta = definition.attributes.get(element)
                if meta is None:
                    continue
                reason = present.lacks(meta, value)
                if reason is not None:
                    # Not a write for the node to refuse: the hub knows from the
                    # descriptor, so the file is what is wrong (LB-HUB-026).
                    self._issue(node, f"ep{number}: {reason} — remove it from the configuration")
                    continue
                out.append((number, meta, value))
        return out

    # --- step by step ---------------------------------------------------------

    def _step(self, task: Task) -> None:
        if not task.pending:
            del self._tasks[bytes(task.node.uid)]
            return
        endpoint, meta, value = task.pending[0]
        self._request(
            task.node.bus,
            task.node.address,
            endpoint,
            meta.id,
            MessageKind.READ,
            b"",
            lambda outcome: self._compare(task, endpoint, meta, value, outcome),
        )

    def _compare(
        self,
        task: Task,
        endpoint: int,
        meta: AttributeDef,
        value: Value,
        outcome: Outcome,
    ) -> None:
        if not self._live(task):
            return
        if not outcome.answered:
            self._abandon(task, f"ep{endpoint} {meta.name}: the node does not answer")
            return
        if outcome.result == ResultCode.UNSUPPORTED_ELEMENT and meta.optional:
            # Not a failure: the device sets this itself (types/0x0010-key-input.md
            # §2.4). CORE has already remembered it in `absent`, as it does for every
            # reply, so the API refuses it from now on without asking the node
            # (zadania/zrobione/CR-01.md, N10; zadania/zrobione/FEAT-06.md, R9). What is left
            # here is reconciling's own part: the file asks for something that is not.
            task.pending.pop(0)
            self._issue(
                task.node,
                f"ep{endpoint}: {task.node.endpoint(endpoint).lacks(meta)} — "
                f"remove it from the configuration",
            )
            self._step(task)
            return
        if outcome.result != ResultCode.OK:
            task.pending.pop(0)
            self._issue(task.node, f"ep{endpoint} {meta.name}: read → {outcome.result}")
            self.failed += 1
            self._step(task)
            return

        self.checked += 1
        try:
            present_value = decode(meta.type, outcome.data)
        except ValueError:
            present_value = None
        if present_value == value:
            task.pending.pop(0)
            self._step(task)
            return

        self.mismatched += 1
        self._request(
            task.node.bus,
            task.node.address,
            endpoint,
            meta.id,
            MessageKind.WRITE,
            encode(meta.type, value),
            lambda result: self._wrote(task, endpoint, meta, result),
        )

    def _wrote(self, task: Task, endpoint: int, meta: AttributeDef, outcome: Outcome) -> None:
        if not self._live(task):
            return
        task.pending.pop(0)
        if not outcome.answered:
            self._abandon(task, f"ep{endpoint} {meta.name}: the node does not answer")
            return
        if outcome.result == ResultCode.OK:
            self.written += 1
            task.written += 1
        else:
            self.failed += 1
            self._issue(task.node, f"ep{endpoint} {meta.name}: write → {outcome.result}")
        self._step(task)

    def _abandon(self, task: Task, reason: str) -> None:
        self._tasks.pop(bytes(task.node.uid), None)
        self.failed += 1
        self._issue(task.node, reason)

    def _issue(self, node: NodeModel, content: str) -> None:
        name = node.name or node.uid.hex()
        self._on_issue(f"{name} (0x{node.address:03X}): {content}")
