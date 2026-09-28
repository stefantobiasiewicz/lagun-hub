"""Persistence of the installation model.

**The database is a backup and a starting point, never a source of answers while
running** ([`ARCHITEKTURA.md`](../../ARCHITEKTURA.md) §6). The model lives in memory;
it reaches the disk with a delay and never sits on a frame’s path.

It is worth remembering what this database is **not**: a copy of the configuration.
The configuration
lives in files and it is the one thing that cannot be rebuilt
([`KONFIGURACJA.md`](../../KONFIGURACJA.md) §1). The database rebuilds itself within
one `ReportInterval` — the file never rebuilds at all.
"""

from __future__ import annotations

from typing import Protocol

from lagunhub.core.model import Installation


class Store(Protocol):
    """Writing and reading the model. Everything keyed by `Uid`, never by address."""

    def save(self, installation: Installation) -> None: ...

    def load(self) -> Installation: ...

    def close(self) -> None: ...
