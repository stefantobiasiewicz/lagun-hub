"""The hub server’s token — the resolution of point **H6**.

The hub controls physical things: a light, a gate, a valve. A server without
authentication on a home network means **everybody on that network controls the house
and configures it** — and in practice every thing on that network too, the television
included.

`zigbee2mqtt` keeps `auth_token` in the open in `configuration.yaml` and has none by
default. We take the first and reject the second, because "off by default" means in
practice "off at everybody’s":

* an empty token in the configuration **does not mean "no token"** — it means "draw one
  and save it",
* the token file has mode `0600` and is in `.gitignore`, because a configuration is
  sometimes kept in git,
* the comparison goes through `compare_digest`, so it cannot be guessed by timing.

What is **deliberately absent**: accounts, roles and passwords. One token per hub is
enough for what the hub does today; roles will come when a second kind of user does.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from pathlib import Path
from typing import Final

TOKEN_FILE: Final = "api-token"
TOKEN_BYTES: Final = 32


class TokenError(RuntimeError):
    """The token can neither be read nor written. The hub will not expose a server without
    one."""


def resolve(token: str, where: Path | None) -> tuple[str, bool]:
    """A token from the configuration, from a file, or freshly drawn.

    Returns ``(token, is_new)`` — because a new one has to be shown to a person once, at
    start-up, and never again.
    """
    if token:
        return token, False
    if where is None:
        return secrets.token_urlsafe(TOKEN_BYTES), True
    file_ = where / TOKEN_FILE
    if file_.exists():
        written = file_.read_text(encoding="utf-8").strip()
        if written:
            _warn_if_loose(file_)
            return written, False
    fresh = secrets.token_urlsafe(TOKEN_BYTES)
    _write(file_, fresh)
    return fresh, True


def matches(expected: str, given: str | None) -> bool:
    """A constant-time comparison. `None` — that is, a missing header — never passes."""
    if not given:
        return False
    # `compare_digest` takes `str` in ASCII only and raises `TypeError` otherwise — so a
    # header with `é` dropped the connection instead of getting 401 (zadania/zrobione/BUG-10.md).
    # As UTF-8 bytes any text compares, still in constant time.
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))


def _write(file_: Path, token: str) -> None:
    """A write with mode `0600` from the start, not after the fact.

    The order matters: a file created with default permissions and fixed up with
    `chmod` is readable by everyone for a moment, and that moment is enough.
    """
    try:
        file_.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(file_, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            out.write(token + "\n")
    except OSError as exc:
        raise TokenError(f"cannot write {file_}: {exc}") from None


def _warn_if_loose(file_: Path) -> None:
    mode = stat.S_IMODE(file_.stat().st_mode)
    if mode & 0o077:
        file_.chmod(0o600)


__all__ = ["TOKEN_FILE", "TokenError", "matches", "resolve"]
