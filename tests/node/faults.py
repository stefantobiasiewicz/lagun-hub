"""Injecting faults.

A node double that always works correctly will not exercise half of the hub's code:
transaction retries, detecting absence, or dropping segments. Every fault here answers
one thing the hub has to be able to cope with.

Here is what the hub's transaction tests need.
"""

from __future__ import annotations

from dataclasses import dataclass

from lagunhub.protocol.registry import ResultCode


@dataclass
class Faults:
    """What is to go wrong. The counters go down with every use."""

    drop_responses: int = 0
    """This many of the next replies will be lost **after the request is carried out**.

    This is the hard case: the command happened and the hub does not know it. The
    repetition has to land in the deduplication buffer rather than carry it out twice.
    """

    busy_responses: int = 0
    """This many of the next requests will get `BUSY` instead of being carried out."""

    silent_until: float = 0.0
    """Until this moment the node sends nothing — neither replies nor reports."""

    drop_segment: int | None = None
    """The number of the segment that will be lost. Used up once.

    A hole in the numbering is to discard the value **as a whole** — there is no partial
    retransmission, so the hub has to repeat the entire request
    (05-mapowanie-can.md §6.6)."""

    def take_drop(self) -> bool:
        if self.drop_responses <= 0:
            return False
        self.drop_responses -= 1
        return True

    def take_busy(self) -> ResultCode | None:
        if self.busy_responses <= 0:
            return None
        self.busy_responses -= 1
        return ResultCode.BUSY

    def take_drop_segment(self, number: int) -> bool:
        if self.drop_segment != number:
            return False
        self.drop_segment = None
        return True

    def silent(self, now: float) -> bool:
        return now < self.silent_until
