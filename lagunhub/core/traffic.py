"""Traffic on the bus — the same thing an analyser shows, only with names.

It differs from [`events.py`](events.py) in what it is about: **an event says what
happened in the installation; traffic says what travelled the wire.** The first is
enough to draw the state; the second is needed to understand why the state is what it
is — it shows retries, `tid`s, refusals and reply times.

**CORE expands the names, and only when somebody is listening.** At five hundred
frames per second, turning `0x001 ep4 0x02` into `kitchen-relay ep4 Output.Toggle`
costs more than the announcement itself — and most of the time nobody is watching.
"""

from __future__ import annotations

from dataclasses import dataclass

from lagunhub.core.events import Bus
from lagunhub.core.model import Uid


@dataclass(frozen=True, slots=True)
class Traffic:
    """One frame, expanded as far as the hub is able."""

    at: float
    bus: str
    kind: str
    """The kind's name: `COMMAND`, `RESPONSE`, `REPORT`, `ANNOUNCEMENT`…"""

    outgoing: bool
    """Whether the hub sent it. Direction is the first thing the eye looks for."""

    address: int = 0
    endpoint: int = 0
    element: int = 0
    uid: Uid | None = None
    name: str = ""
    """`Toggle`, `OnOff`, `Click` — expanded from the type catalogue when the hub knows
    the endpoint."""

    detail: str = ""
    """Whatever cannot be taken apart into fields: a value, a reason, a `ShortId`."""

    tid: int | None = None
    result: str | None = None
    elapsed: float | None = None
    """Time since the first attempt of the request — on replies, and on retries. A reply
    to a retry shows how long the hub has really been waiting."""

    late: bool = False
    """A reply that came after its transaction closed — an answer to a retry, or one
    after the hub gave up (zadania/BUG-03.md). A sign of latency on the bus worth seeing
    when hunting a fault."""

    retry: int = 0
    """On a request the hub sends: which retry this is, 0 for the first attempt. On the
    wire a retry is indistinguishable — the same frame, the same `Tid`."""

    data: bytes = b""


TrafficBus = Bus[Traffic]

__all__ = ["Traffic", "TrafficBus"]
