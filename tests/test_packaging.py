"""Whether what is on disk reaches the installation.

This test came about after it turned out that **the wheel did not contain
`lagunhub.adapters`** — that is, `pip install lagunhub` gave a hub without a server and
without a shell. A development install does not show that, because it reads straight from
the directory: the fault was invisible for two stages.

Since the node double left the package there is a second question: whether anything gets
into the installation that is not to. On a Raspberry Pi the hub is to land and nothing
else — the double lives in `tests/node/` and is a test tool.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = {"lagunhub": ROOT / "lagunhub"}


def _packages() -> list[str]:
    out: list[str] = []
    for name, root in SOURCES.items():
        for init in sorted(root.rglob("__init__.py")):
            relative = init.parent.relative_to(root).parts
            out.append(".".join([name, *relative]))
    return out


@pytest.mark.parametrize("name", _packages())
def test_every_package_can_be_imported(name: str) -> None:
    importlib.import_module(name)


def test_the_package_list_is_discovered_not_transcribed() -> None:
    """A hand-written list drifts silently at the first new package.

    This test does not check the wheel's contents — that would need building it — only
    that nobody went back to transcribing the list by hand.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = pyproject["tool"]["setuptools"]["packages"]
    assert isinstance(packages, dict), (
        "the package list went back to being hand-written — see this file's docstring"
    )
    assert packages["find"]["where"] == ["."]


def test_the_package_holds_the_hub_alone() -> None:
    """The node double has no right to enter the installation.

    As long as it sat in `simulators/` and was listed in `include`, `pip install lagunhub`
    put it on the machine together with the hub. That is exactly what was to go away — so
    let a test watch it rather than memory.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["setuptools"]["packages"]["find"]["include"] == ["lagunhub*"]
    assert list(pyproject["project"]["scripts"]) == ["lagun-hub"]
