"""`lagun-hub init` — a clean configuration for a real installation.

The `config/` directory in the repository is **an example to read**: it holds two
particular `Uid`s that exist nowhere but in the tests. A real house starts empty and
fills itself — nodes come in through joining, and a human names them from the view.

Three files, because that is how the configuration divides
([`KONFIGURACJA.md`](../KONFIGURACJA.md) §1): the machine's settings, the decisions about
nodes, and the bindings. The comments stay in the files, because the hub writes them
through `ruamel` in round-trip mode and they **survive the first write from the API** —
that is what they are there for.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from lagunhub.core.config.loader import FILES

HUB: Final = """\
# The hub's own settings — the things that are THIS MACHINE's business.
# Everything about the installation sits in nodes.yaml and bindings.yaml.

buses:
  - name: {name}
    kind: socketcan
    channel: {channel}
    bitrate: {bitrate}

# The hub sets the interface up to this at every start (ip link, through sudo -n).
# What it cannot do by itself — no such interface, no sudo rule — shows up as an issue.

# Groups: the key is the NUMBER, because that is what travels in a frame's ADDRESS field.
# The name is a label — the same rule as with nodes, where the key is the Uid.
groups: {{}}

# The mode for accepting new nodes. Leave it off and open it deliberately —
# from the view with the [a] key, or through the API. The window expires on its own.
accept_new: false

api:
  # Loopback. Change it to 0.0.0.0 to reach the hub from another machine — but remember
  # that the token is then the ONLY protection, and the hub drives a gate and a lock.
  host: 127.0.0.1
  port: 8080
"""

NODES: Final = """\
# Nodes — the key is the Uid, because that is what identity is. There are no addresses
# here and there will not be: an address is a route, not an identity, and the hub
# assigns it itself at joining.
#
# This file fills itself. When a new node enters the installation, give it a name and an
# alias from the view (`lagun-hub tui`, keys [n] and [N]) — they will land here.

nodes: {}
"""

BINDINGS: Final = """\
# Bindings: when this happens, do that. Both sides are pointed at by ALIAS, which the hub
# translates into a Uid on load — so a node has to have an alias in nodes.yaml.
#
# They are added from the view (`lagun-hub tui`, the Bindings section, key [+]) or here:
#
#   - from: { node: key-kitchen,  endpoint: 4, type: KeyInput, event: Click }
#     to:   { node: lamp-kitchen, endpoint: 4, type: Output,   command: Toggle }

bindings: []
"""


class InitError(RuntimeError):
    """A configuration cannot be set up in this place."""


def init(where: Path, *, channel: str = "can0", bitrate: int = 500_000) -> list[Path]:
    """Set up a clean configuration. **It overwrites nothing** — that is the whole
    protection.

    Returns the list of files created.
    """
    existing = [where / name for name in FILES if (where / name).exists()]
    if existing:
        names = ", ".join(p.name for p in existing)
        raise InitError(
            f"{where} already has a configuration ({names}) — I am touching nothing. "
            f"Point at another directory, or remove those files deliberately."
        )
    try:
        where.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InitError(f"cannot set up {where}: {exc}") from None

    content = {
        "hub.yaml": HUB.format(name=channel, channel=channel, bitrate=bitrate),
        "nodes.yaml": NODES,
        "bindings.yaml": BINDINGS,
    }
    created: list[Path] = []
    for name, payload_text in content.items():
        file_ = where / name
        file_.write_text(payload_text, encoding="utf-8")
        created.append(file_)
    return created


def advice(where: Path) -> str:
    """What to do with what has just come into being."""
    path = where if where.is_absolute() else Path(os.getcwd()) / where
    return (
        f"\nConfiguration in {path}\n"
        f"\nTo have the hub use it without repeating --config with every command:\n"
        f"  echo 'export LAGUN_CONFIG={path}' >> ~/.bashrc && exec bash\n"
        f"\nThen:\n"
        f"  lagun-hub run\n"
        f"\nThe installation starts empty. Nodes will come in by themselves once you open\n"
        f"accepting — from the view (`lagun-hub tui`, key [a]) or in hub.yaml."
    )


__all__ = ["InitError", "advice", "init"]
