"""The hub API — a named list of operations, shared by every adapter.

Split into three by what depends on what:

| module | what | depends on `core/` |
|---|---|---|
| `errors.py` | `Problem`, `ApiError` | no |
| `views.py` | the frozen dataclasses the operations return | no |
| `hub.py` | `HubApi` — the operations and the road to CORE | yes |

The first two are the **contract** with the adapters; a client of a running hub needs them
and nothing else. Everything is re-exported here, so `from lagunhub.api import NodeView`
works as it always did — but a client that wants to stay light imports from
`lagunhub.api.views` directly.

**`hub.py` is loaded lazily**, on first use of `HubApi` and company. Python runs this file
before any submodule, so an eager import here would drag the whole core into
`import lagunhub.api.views` — the very thing the split is for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lagunhub.api.errors import ApiError, Problem
from lagunhub.api.views import (
    ActionView,
    ArgView,
    AttributeView,
    BindingView,
    BusView,
    CommandView,
    CommissioningView,
    ConfigView,
    DiffRow,
    EndpointView,
    EventView,
    HealthView,
    HubInfo,
    JoinView,
    NodeSummary,
    NodeView,
    Plain,
    ReconcileView,
    TargetView,
    TrafficView,
)

__all__ = [
    "ActionView",
    "ApiError",
    "ArgView",
    "AttributeView",
    "BindingView",
    "BusView",
    "CommandView",
    "CommissioningView",
    "ConfigView",
    "DiffRow",
    "EndpointView",
    "EventStream",
    "EventView",
    "HealthView",
    "HubApi",
    "HubInfo",
    "JoinView",
    "NodeSummary",
    "NodeView",
    "Plain",
    "Problem",
    "ReconcileView",
    "Stream",
    "TargetView",
    "TrafficStream",
    "TrafficView",
    "Waiter",
    "block",
]

_FROM_HUB = frozenset({"EventStream", "HubApi", "Stream", "TrafficStream", "Waiter", "block"})

if TYPE_CHECKING:
    from lagunhub.api.hub import EventStream, HubApi, Stream, TrafficStream, Waiter, block


def __getattr__(name: str) -> Any:
    if name in _FROM_HUB:
        from lagunhub.api import hub

        return getattr(hub, name)
    raise AttributeError(f"module 'lagunhub.api' has no attribute {name!r}")
