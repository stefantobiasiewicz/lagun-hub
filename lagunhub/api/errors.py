"""Errors of the hub API — the kinds of failure and the one exception type.

Part of the contract with the adapters, together with the views: whoever talks to the hub
— over HTTP or directly — gets the same `Problem` and translates it into its own terms.
Depends on nothing in the hub.
"""

from __future__ import annotations

from enum import Enum


class Problem(Enum):
    """The kind of failure. An adapter translates it into its own: HTTP into a code, the
    CLI into a sentence."""

    NOT_FOUND = "not_found"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"
    """The node is known, but there is no way to talk to it — usually it has no
    address."""

    REFUSED = "refused"
    """The node answered, and with a refusal. The result code is in `result`."""

    SILENT = "silent"
    """Three attempts without an answer. Different from a refusal and different from a hub
    error."""

    TIMEOUT = "timeout"
    """The **hub** did not answer, not the node. This is a fault on our side."""

    CONFLICT = "conflict"
    """The configuration file changed since the asker saw it.

    Refusing is the only honest answer: a silent win for the last write would erase
    somebody else’s change without a trace
    ([`KONFIGURACJA.md`](../KONFIGURACJA.md) §11.2)."""

    REJECTED = "rejected"
    """The content is syntactically fine, but the configuration as a whole does not hold
    up.

    Different from `INVALID`: there the request is broken, here the request is fine and
    it is the **result** that does not hold up."""


class ApiError(Exception):
    def __init__(self, problem: Problem, message: str, *, result: str | None = None) -> None:
        super().__init__(message)
        self.problem = problem
        self.message = message
        self.result = result


__all__ = ["ApiError", "Problem"]
