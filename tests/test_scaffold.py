"""`lagun-hub init` — a clean configuration for a real installation.

`config/` in the repository is **an example to read**: it holds two `Uid`s that exist
nowhere but in the tests. A real house starts empty — and that is the only thing these
tests check, but in several dimensions.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lagunhub import scaffold
from lagunhub.core.config import CONFIG_ENV, BusKind, default_config, load
from lagunhub.core.config.loader import FILES


class TestCreating:
    def test_all_three_files_are_created(self, tmp_path: Path) -> None:
        created = scaffold.init(tmp_path / "house")
        assert [p.name for p in created] == list(FILES)
        assert all(p.exists() for p in created)

    def test_the_directory_is_created_along_with_its_path(self, tmp_path: Path) -> None:
        scaffold.init(tmp_path / "a" / "b" / "c")
        assert (tmp_path / "a" / "b" / "c" / "hub.yaml").exists()

    def test_it_does_not_overwrite_an_existing_one(self, tmp_path: Path) -> None:
        """The whole protection of this command. A house configuration is hours of a
        human's work."""
        scaffold.init(tmp_path)
        with pytest.raises(scaffold.InitError, match="already has a configuration"):
            scaffold.init(tmp_path)

    def test_the_refusal_names_what_stood_in_the_way(self, tmp_path: Path) -> None:
        (tmp_path / "nodes.yaml").write_text("nodes: {}\n", encoding="utf-8")
        with pytest.raises(scaffold.InitError, match=r"nodes\.yaml"):
            scaffold.init(tmp_path)


class TestContents:
    def test_it_loads_and_is_empty(self, tmp_path: Path) -> None:
        scaffold.init(tmp_path, channel="can0", bitrate=500_000)
        config = load(tmp_path)
        assert config.nodes == ()
        assert config.bindings == ()
        assert config.groups == {}

    def test_the_bus_comes_from_the_argument(self, tmp_path: Path) -> None:
        scaffold.init(tmp_path, channel="vcan0", bitrate=250_000)
        (bus,) = load(tmp_path).buses
        assert (bus.channel, bus.bitrate) == ("vcan0", 250_000)

    def test_joining_starts_closed(self, tmp_path: Path) -> None:
        """A fresh hub in someone else's network has no right to let anything in by
        itself."""
        scaffold.init(tmp_path)
        assert load(tmp_path).accept_new is False

    def test_the_api_listens_on_the_loopback(self, tmp_path: Path) -> None:
        scaffold.init(tmp_path)
        assert load(tmp_path).api.host == "127.0.0.1"

    def test_the_files_are_half_comment(self, tmp_path: Path) -> None:
        """That is what `ruamel` round-trip is for: a fresh file explains what goes
        where, and that has to survive the first write from the API."""
        scaffold.init(tmp_path)
        for name in FILES:
            content = (tmp_path / name).read_text(encoding="utf-8")
            comments = [line for line in content.split("\n") if line.startswith("#")]
            assert len(comments) >= 3, name

    def test_the_bus_is_written_with_its_kind(self, tmp_path: Path) -> None:
        """The shape from zadania/FEAT-01.md (R6). There used to be an `ip link` command
        in a comment here for a person to copy; since the hub sets the interface up by
        itself (R3) there is nothing to copy."""
        scaffold.init(tmp_path, channel="can1", bitrate=250_000)
        content = (tmp_path / "hub.yaml").read_text(encoding="utf-8")
        assert "kind: socketcan" in content
        assert "ip link set can1" not in content
        (bus,) = load(tmp_path).buses
        assert bus.kind is BusKind.SOCKETCAN


class TestDirectoryLookup:
    def test_the_environment_variable_points_at_the_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CONFIG_ENV, "/house/lagun")
        assert default_config() == Path("/house/lagun")

    def test_without_the_variable_the_current_directory_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CONFIG_ENV, raising=False)
        assert default_config() == Path("config")

    def test_an_empty_variable_does_not_yield_an_empty_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CONFIG_ENV, "")
        assert default_config() == Path("config")

    def test_the_advice_gives_an_absolute_path(self, tmp_path: Path) -> None:
        """A line to paste into `.bashrc` — with a relative path it would be a trap."""
        text = scaffold.advice(tmp_path)
        assert f"export {CONFIG_ENV}={tmp_path}" in text
        assert os.path.isabs(str(tmp_path))
