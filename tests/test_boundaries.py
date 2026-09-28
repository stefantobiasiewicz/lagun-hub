"""The boundary between the hub's core and its clients — checked, not assumed.

A client of a running hub (the shell, the full-screen view, the HTTP client under them)
talks to it over HTTP and needs nothing from `core/` but the **shapes** of the answers,
which live in `lagunhub.api.views` and `lagunhub.api.errors`. Until the API was split,
importing one view dataclass dragged in the CORE thread, the configuration store and the
binding engine — twenty-two core modules — and nobody noticed, because nothing checked.

Each check runs in a fresh interpreter: in this process the core is long since loaded by
other tests, so `sys.modules` here proves nothing.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

CLIENT_SIDE = (
    "lagunhub.api.views",
    "lagunhub.api.errors",
    "lagunhub.adapters.cli.client",
    "lagunhub.adapters.cli.commands",
    "lagunhub.adapters.cli.render",
    "lagunhub.adapters.cli.shell",
    "lagunhub.adapters.tui.app",
)

PROBE = """
import sys, importlib
importlib.import_module({module!r})
core = sorted(m for m in sys.modules if m == "lagunhub.core" or m.startswith("lagunhub.core."))
print(" ".join(core))
"""


@pytest.mark.parametrize("module", CLIENT_SIDE)
def test_the_client_side_does_not_load_the_core(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(module=module)],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = result.stdout.split()
    assert loaded == [], f"{module} loads the core: {', '.join(loaded)}"


def test_the_hub_api_is_still_reachable_from_the_package() -> None:
    """`hub.py` loads lazily — which must not mean it stops being importable."""
    from lagunhub.api import HubApi, Stream, block

    assert HubApi.__module__ == "lagunhub.api.hub"
    assert Stream.__module__ == "lagunhub.api.hub"
    assert callable(block)
