"""A node working under an address the hub does not know —
06-cykl-zycia-urzadzenia.md §2.3.

A node with an address in persistent storage does not take part in joining
(03-adresacja.md §5.1), so joining will never bring it in. It happens after the hub's
database is lost or restored from an old backup, after a forced removal whose
`FactoryReset` did not get through, and when a module used elsewhere is plugged in.

**Every frame is the trigger, not `Booted`.** The hub runs on a Raspberry Pi and comes up
minutes after the nodes, so their `Booted` is usually gone by then. Every node reports
`Diagnostics` at least every 300 s (06-cykl-zycia-urzadzenia.md §5), so any stranger shows
up within five minutes — without polling everyone after a restart (07-hub.md §5.1).

The hub reads `Uid` and `ShortId` from that address — one identification per address,
not one per frame — and puts the node in `PENDING`. It never lets it in by itself, not
even with the accepting mode open: the mode is about nodes that announce, i.e. nodes
**without** an address. This one brought a configuration the hub knows nothing about,
and the decision belongs to a person.

**Why `ShortId` is read and not computed from `Uid`.** Computing assumes the node computes
it the same way. Ours did not — it took the four high bytes instead of a CRC-32 — and
nobody saw it until somebody computed it twice (DECYZJE.md, "Węzeł z adresem, którego hub
nie zna").

The rejected alternative — nodes announcing their address at every start, J1939 style —
is described in DECYZJE.md, "Konflikt adresów: zapobieganie zamiast wykrywania".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from lagunhub.core.model import Installation, JoinRecord, NodeState, Uid
from lagunhub.core.transactions import Outcome
from lagunhub.protocol.endpoints import device as dev
from lagunhub.protocol.values import decode, decode_blob

Request = Callable[[int, int, int, int, Callable[[Outcome], None]], None]
"""``(bus, address, endpoint, element, done)`` — the same shape the enumerator uses."""

FIRST_NODE: Final = 0x001
LAST_NODE: Final = 0x7FD
"""Node addresses (03-adresacja.md §2). The hub and broadcast are not strangers."""

RETRY: Final = 60.0
"""How long after a failed identification the next frame may start another.

Without it a node that does not answer — or two nodes under one address whose replies
collide — would start a read on every frame they send. A minute costs nothing: the node
has been unknown for minutes anyway."""


@dataclass
class Probe:
    address: int
    bus: int
    uid: Uid | None = None


class Strangers:
    """Identifies nodes under unknown addresses. Belongs solely to the CORE thread."""

    def __init__(
        self,
        installation: Installation,
        request: Request,
        clock: Callable[[], float],
        *,
        on_pending: Callable[[JoinRecord], None] | None = None,
        on_uid_conflict: Callable[[int, Uid, int], None] | None = None,
    ) -> None:
        self.installation = installation
        self._request = request
        self._clock = clock
        self._on_pending = on_pending
        self._on_uid_conflict = on_uid_conflict
        self._probes: dict[int, Probe] = {}
        self._not_before: dict[int, float] = {}
        self._settled: set[int] = set()
        """Addresses already reported as a contradiction of the database. Probing them
        again would find the same thing and report it on every frame."""

        self.probed = 0
        self.pending = 0
        self.blocked = 0
        self.failed = 0

    def __len__(self) -> int:
        return len(self._probes)

    def heard(self, bus: int, address: int) -> None:
        """A frame **from a node** arrived — a report, event, alarm, reply or query."""
        if not FIRST_NODE <= address <= LAST_NODE:
            return
        # Heard is taken, whatever the database says (03-adresacja.md §6). This comes
        # first, before any "do I know it", because it is exactly the address the
        # database got wrong that must not be handed out.
        self.installation.addresses.hear(address)
        if self.installation.holder(address) is not None:
            return
        if address in self._probes or address in self._settled:
            return
        now = self._clock()
        if now < self._not_before.get(address, 0.0):
            return
        probe = Probe(address=address, bus=bus)
        self._probes[address] = probe
        self.probed += 1
        self._request(bus, address, 0, dev.UID, lambda outcome: self._got_uid(probe, outcome))

    def forget(self, address: int) -> None:
        """The node under this address was reset to factory settings — the address is
        free again, and a later frame from it is a new story."""
        self._settled.discard(address)
        self._not_before.pop(address, None)
        self.installation.addresses.unhear(address)

    # --- step by step ---------------------------------------------------------

    def _got_uid(self, probe: Probe, outcome: Outcome) -> None:
        if not outcome.ok:
            self._fail(probe)
            return
        probe.uid = Uid(decode_blob(outcome.data))
        known = self.installation.nodes.get(probe.uid)
        if known is not None:
            # The hub knows this device under a different address: the database and the
            # bus disagree — a backup older than a readdressing, say. The hub does not
            # guess which is current; it shows it.
            self._settle(probe)
            if self._on_uid_conflict is not None:
                self._on_uid_conflict(probe.address, probe.uid, known.address)
            return
        self._request(
            probe.bus,
            probe.address,
            0,
            dev.SHORT_ID,
            lambda outcome: self._got_short_id(probe, outcome),
        )

    def _got_short_id(self, probe: Probe, outcome: Outcome) -> None:
        value = decode("u32", outcome.data[:4]) if outcome.ok and len(outcome.data) >= 4 else None
        if not isinstance(value, int) or isinstance(value, bool):
            self._fail(probe)
            return
        short_id = value
        record = self.installation.record(short_id)
        if record.address:
            # A `ShortId` the hub holds under another address, with a `Uid` it does not
            # know — the same contradiction as above, seen from the other key.
            self._settle(probe)
            self.installation.issues.append(
                f"a node at 0x{probe.address:03X} reports ShortId 0x{short_id:08X}, which "
                f"the hub holds at 0x{record.address:03X} — the database and the bus "
                f"disagree"
            )
            return
        del self._probes[probe.address]
        record.address = probe.address
        record.bus = probe.bus
        record.uid = probe.uid
        record.last_seen = self._clock()
        self.installation.addresses.reserve(probe.address)
        if self.installation.is_blocked(probe.uid):
            # Recognised, and that is all: no operator decision to wait for, the file
            # already holds it (06-cykl-zycia-urzadzenia.md §8). The record keeps the
            # address so the next frame from it is not a stranger's.
            record.state = NodeState.BLOCKED
            self.blocked += 1
            return
        record.state = NodeState.PENDING
        self.pending += 1
        if self._on_pending is not None:
            self._on_pending(record)

    def _settle(self, probe: Probe) -> None:
        self._probes.pop(probe.address, None)
        self._settled.add(probe.address)

    def _fail(self, probe: Probe) -> None:
        self._probes.pop(probe.address, None)
        self._not_before[probe.address] = self._clock() + RETRY
        self.failed += 1
