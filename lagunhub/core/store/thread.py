"""The STORE thread — writing with a delay.

It exists so that `fsync` on an SD card does not stall CORE. A card can go quiet for
tens to hundreds of milliseconds, and in that time CORE has to mind transaction
retries on a hundred-millisecond budget
([`ARCHITEKTURA.md`](../../ARCHITEKTURA.md) §5.2).

Requests **coalesce**: when several wait in the queue, only the last is written.
Writing intermediate model states has no value at all, and during a wave of joins it
would be forty writes instead of one.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from lagunhub.core.model import Installation
from lagunhub.core.store.base import Store

Snapshotter = Callable[[], Installation]


class StoreThread(threading.Thread):
    def __init__(self, store: Store, *, name: str = "store") -> None:
        super().__init__(name=name, daemon=True)
        self.store = store
        self._queue: queue.Queue[Installation | None] = queue.Queue()
        self.saved = 0
        self.coalesced = 0

    def request_save(self, installation: Installation) -> None:
        """Called from CORE. Does not block and does not touch the disk."""
        self._queue.put(installation)

    def run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            while True:  # coalesce everything already waiting
                try:
                    more = self._queue.get_nowait()
                except queue.Empty:
                    break
                if more is None:
                    self._write(item)
                    return
                item = more
                self.coalesced += 1
            self._write(item)

    def _write(self, installation: Installation) -> None:
        self.store.save(installation)
        self.saved += 1

    def stop(self) -> None:
        self._queue.put(None)
