"""Writing the configuration — the file is the source of truth and the API changes it.

All of it follows from one sentence in
[`KONFIGURACJA.md`](../../../KONFIGURACJA.md) §11.1:

> **Configuration always goes through the file. The API writes the file, not the node.**

It is tempting to let a `HoldThreshold` write from the app go straight to the node — it
would be shorter and the effect would show at once. But that would be a **second route
to the same thing**, and reconciling (§8) would undo such a write at the first check,
and would be right to.

Three things this module does that have to be done right:

* **order** — validation, then the file, and only then memory (§11.3). Of the two
  possible
  a crash, "written, not applied" is fixed by a restart, while "applied, not written"
  disappears quietly at the next power cut,
* **`ETag`** — the file has two writers, the app and a person with an editor, so a
  write is check-and-set. A silent win for the last one would erase somebody else’s
  change without a trace (§11.2),
* **comments** — `ruamel.yaml` in round-trip mode. A plain `yaml.dump()` erases
  comments and key order, so after the first write from the app the file stops being
  what it was a file for (§11.5).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from io import StringIO
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML

from lagunhub.core.config.loader import FILES, ConfigError, from_document
from lagunhub.core.config.model import Config

DOMY: Final[dict[str, str]] = {
    "nodes": "nodes.yaml",
    "bindings": "bindings.yaml",
}
"""Which file a top-level key lands in. The rest go to `hub.yaml`."""

DEFAULT_FILE: Final = "hub.yaml"


class ConflictError(ConfigError):
    """The file changed since the asker last saw it.

    Refusing is the **only honest answer**: somebody edited the file by hand or from
    another tab, and their change would vanish without a trace (§11.2).
    """


class ConfigStore:
    """A configuration directory or single file, with a version and an atomic write."""

    def __init__(self, where: Path) -> None:
        self.where = where
        self._yaml = YAML()  # round-trip: comments and order survive a write
        self._yaml.preserve_quotes = True
        self._etag = self.digest()

    # --- version -------------------------------------------------------------

    @property
    def etag(self) -> str:
        """The digest the hub last issued or wrote."""
        return self._etag

    def files(self) -> list[Path]:
        if self.where.is_dir():
            return [self.where / name for name in FILES if (self.where / name).exists()]
        return [self.where] if self.where.exists() else []

    def digest(self) -> str:
        """A digest of the **content**, not of the modification time.

        `mtime` serves only to avoid computing the digest needlessly: an editor that
        saved the file unchanged is not to set off reconciling the whole installation.
        """
        digest = hashlib.sha256()
        for file_ in self.files():
            digest.update(file_.name.encode("utf-8"))
            digest.update(file_.read_bytes())
        return f"sha256:{digest.hexdigest()[:32]}"

    def changed(self) -> bool:
        """Whether somebody changed the files **outside the hub**. Our own write is not a
        change."""
        return self.digest() != self._etag

    # --- reading -------------------------------------------------------------

    def document(self) -> dict[str, Any]:
        """The merged content of every file, in round-trip form."""
        merged: dict[str, Any] = {}
        for file_ in self.files():
            part = self._yaml.load(file_)
            if part:
                merged.update(part)
        return merged

    def mark_seen(self) -> None:
        """Remember the current content as seen, **without loading it**.

        Needed after rejecting a broken file: without it the hub would append the same
        issue every second. The next fix in the file will change the digest again and be
        noticed again — so nothing is lost but the repetition.
        """
        self._etag = self.digest()

    def load(self) -> Config:
        config = from_document(self.document())
        self._etag = self.digest()
        return config

    # --- writing -------------------------------------------------------------

    def set(self, path: tuple[str, ...], value: Any, *, etag: str) -> Config:
        """Set a value at a path in the document. Returns the resulting `Config`."""
        return self.set_many([(path, value)], etag=etag)

    def set_many(self, changes: Sequence[tuple[tuple[str, ...], Any]], *, etag: str) -> Config:
        """Several values in **one write**: one validation, one file, one `ETag`.

        Needed where one change entails another. An endpoint attribute requires a `type`
        to stand in the file ([`KONFIGURACJA.md`](../../../KONFIGURACJA.md) §10) — a
        write in two steps would then be **invalid half way through**: the first would
        fail validation, and had it passed, the file would be left with an attribute
        without a type.
        """
        return self._edit(changes, etag=etag, remove=False)

    def delete(self, path: tuple[str, ...], *, etag: str) -> Config:
        """Delete a key. The comment beside it **goes with it**, and so it should — a comment
        describing a setting that no longer exists is worse than none (§11.5)."""
        return self._edit([(path, None)], etag=etag, remove=True)

    def _edit(
        self,
        changes: Sequence[tuple[tuple[str, ...], Any]],
        *,
        etag: str,
        remove: bool,
    ) -> Config:
        if not changes or any(not path for path, _ in changes):
            raise ConfigError("the write path cannot be empty")
        latest = self.digest()
        if etag != latest:
            raise ConflictError(
                f"the file changed since {etag}; it is now {latest} — refresh and try again"
            )

        # Every change from one write has to land in the same file — otherwise "one
        # validation, one write" stops being true.
        files = {self._home(path[0]) for path, _ in changes}
        if len(files) > 1:
            raise ConfigError("one write cannot touch two files at once")
        file_ = files.pop()

        payload_text: Any = self._yaml.load(file_) if file_.exists() else {}
        if not isinstance(payload_text, dict):
            raise ConfigError(f"{file_.name}: expected a mapping at the top level")
        for path, value in changes:
            _poke(payload_text, path, value, remove=remove)

        # Step 1: validate the WHOLE document, not just the file — because a binding in
        # bindings.yaml refers to an alias from nodes.yaml.
        attempt = {**self.document(), **payload_text}
        config = from_document(attempt)

        # Step 2: only now the file. Until here nothing changed.
        _atomic_write(file_, self._dump(payload_text))
        self._etag = self.digest()
        return config

    def forget_node(self, uid: bytes, *, etag: str) -> tuple[Config, int]:
        """Remove a node's entry **and every binding it takes part in** — the file half
        of removing a node (06-cykl-zycia-urzadzenia.md §6). Returns the new
        configuration and how many bindings went.

        Bindings go with it because they name the node by alias, and a binding naming an
        alias nobody holds fails the whole file. A binding with several targets loses
        only this node's; one left with none goes entirely.

        **Two files in one operation** — the one place `_edit`'s "one write, one file"
        does not hold. The order makes up for it: bindings are written first, so a crash
        between the two writes leaves a node without bindings, which loads, rather than
        bindings without their node, which does not.
        """
        latest = self.digest()
        if etag != latest:
            raise ConflictError(
                f"the file changed since {etag}; it is now {latest} — refresh and try again"
            )
        nodes_file = self._home("nodes")
        bindings_file = self._home("bindings")
        nodes_doc: Any = self._yaml.load(nodes_file) if nodes_file.exists() else {}
        bindings_doc: Any = (
            nodes_doc
            if bindings_file == nodes_file
            else (self._yaml.load(bindings_file) if bindings_file.exists() else {})
        )
        for file_, doc in ((nodes_file, nodes_doc), (bindings_file, bindings_doc)):
            if not isinstance(doc, dict):
                raise ConfigError(f"{file_.name}: expected a mapping at the top level")

        wanted = bytes(uid).hex()
        nodes = nodes_doc.get("nodes") or {}
        key = next((k for k in nodes if str(k).lower() == wanted), None)
        names = {wanted}
        if key is not None:
            entry = nodes[key] or {}
            names.add(str(entry.get("alias", "")) or str(key))
            del nodes[key]

        removed = 0
        bindings = bindings_doc.get("bindings") or []
        for index in reversed(range(len(bindings))):
            binding = bindings[index]
            if _names(binding.get("from"), names):
                del bindings[index]
                removed += 1
                continue
            targets = binding.get("to")
            if isinstance(targets, list):
                for t in reversed(range(len(targets))):
                    if _names(targets[t], names):
                        del targets[t]
                if targets:
                    continue
            elif not _names(targets, names):
                continue
            del bindings[index]
            removed += 1

        merged = {**self.document(), **bindings_doc, **nodes_doc}
        config = from_document(merged)
        if bindings_file != nodes_file:
            _atomic_write(bindings_file, self._dump(bindings_doc))
        _atomic_write(nodes_file, self._dump(nodes_doc))
        self._etag = self.digest()
        return config, removed

    def _home(self, top: str) -> Path:
        """Which file this key belongs to. For a single file — to that one."""
        if not self.where.is_dir():
            return self.where
        for file_ in self.files():
            part = self._yaml.load(file_)
            if isinstance(part, dict) and top in part:
                return file_
        return self.where / DOMY.get(top, DEFAULT_FILE)

    def _dump(self, payload_text: Any) -> str:
        buffer = StringIO()
        self._yaml.dump(payload_text, buffer)
        return buffer.getvalue()


# --- small change ----------------------------------------------------------------


def _poke(document: dict[Any, Any], path: tuple[str, ...], value: Any, *, remove: bool) -> None:
    """Set or delete a value at a path, creating the missing levels on the way."""
    place: dict[Any, Any] = document
    parent = ""
    for i, segment in enumerate(path[:-1]):
        key = _key(place, segment, parent)
        parent = str(segment)
        next_ = place.get(key) if isinstance(place, dict) else None
        if next_ is None:
            if remove:
                raise ConfigError(f"nothing to delete at {'/'.join(path)}")
            next_ = {}
            place[key] = next_
        if not isinstance(next_, dict):
            raise ConfigError(f"{'/'.join(path[: i + 1])} is not a mapping")
        place = next_
    last = _key(place, path[-1], parent)
    if remove:
        if last not in place:
            raise ConfigError(f"nothing to delete at {'/'.join(path)}")
        del place[last]
    else:
        place[last] = value


def _names(end: Any, names: set[str]) -> bool:
    """Whether one end of a binding is this node — by alias or by `Uid`, the two ways the
    loader accepts."""
    if not isinstance(end, dict) or "node" not in end:
        return False  # a group target names no node
    return str(end["node"]) in names or str(end["node"]).lower() in names


NUMERIC: Final = ("endpoints", "groups")
"""**Numbers** live under these keys, not names.

The parent settles this, not the look of the segment itself — and it has to, because
the `Uid` `0001020304050607` looks like a number too, and is not one
([`KONFIGURACJA.md`](../../../KONFIGURACJA.md) §4)."""


def _key(place: dict[Any, Any], segment: str, parent: str) -> Any:
    """A path segment as a document key.

    Endpoint and group numbers are numbers in YAML and strings in a URL — so
    `endpoints/4` is to land on key `4`, not on `"4"`. A Uid stays a string, because in
    the file it is a string too.

    **A key that does not exist yet cannot be recognised by its content** — hence the
    parent. Without it, the first endpoint written into a fresh configuration landed as
    `'4'` and every subsequent one, into an existing map, as `4`: the same file with two
    kinds of keys, differing by what came before.
    """
    if segment in place:
        return segment
    try:
        number = int(segment)
    except ValueError:
        return segment
    if number in place:
        return number
    return number if parent in NUMERIC else segment


def _atomic_write(file_: Path, content: str) -> None:
    """`temp -> fsync -> rename`, so the file is never half written.

    `fsync` on an SD card can go quiet for tens of milliseconds and blocks the CORE
    thread for that long. That is acceptable: configuration changes are rare, and the
    fast path does not go through CORE (§11.3).
    """
    file_.parent.mkdir(parents=True, exist_ok=True)
    temporary = file_.with_suffix(file_.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, file_)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


__all__ = ["ConfigStore", "ConflictError"]
