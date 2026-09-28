"""Writing the configuration — the file is the source of truth
(KONFIGURACJA.md §11).

Three things these tests watch, because breaking any of them is silent:

1. **nothing changes when anything failed** — neither the file nor memory,
2. **the `ETag` is check-and-set**, so somebody else's change does not vanish,
3. **comments survive a write**, because otherwise the file stops being what a file was
   for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lagunhub.core.config.loader import ConfigError
from lagunhub.core.config.store import ConfigStore, ConflictError
from tests.conftest import Clock
from tests.test_enumeration import Rig

HUB = """\
# The hub's own settings — this comment is to survive a write from the API.
buses: [vcan0]

groups:
  1: { name: all-lights }
"""

NODES = """\
# The key is the Uid, because that is what identity is.
nodes:
  "08090a0b0c0d0e0f":
    alias: kitchen-relay        # a label for a human
    name: "The lamp over the table"
    endpoints:
      4:
        type: Output
        Purpose: LIGHT
"""

BINDINGS = """\
bindings: []
"""


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    (tmp_path / "hub.yaml").write_text(HUB, encoding="utf-8")
    (tmp_path / "nodes.yaml").write_text(NODES, encoding="utf-8")
    (tmp_path / "bindings.yaml").write_text(BINDINGS, encoding="utf-8")
    return tmp_path


@pytest.fixture
def store(directory: Path) -> ConfigStore:
    return ConfigStore(directory)


ENDPOINT = "nodes/08090a0b0c0d0e0f/endpoints/4"


class TestVersioning:
    def test_the_etag_comes_from_the_content_not_the_time(
        self, store: ConfigStore, directory: Path
    ) -> None:
        before = store.etag
        (directory / "nodes.yaml").write_text(NODES, encoding="utf-8")  # the same text
        assert store.digest() == before, "a write without changes is not a change"
        assert not store.changed()

    def test_a_write_bumps_the_etag(self, store: ConfigStore) -> None:
        before = store.etag
        store.set(_path(f"{ENDPOINT}/AutoOffTime"), 30, etag=before)
        assert store.etag != before

    def test_our_own_write_is_not_an_outside_change(self, store: ConfigStore) -> None:
        """Otherwise the hub would pick up its own write and reconcile the installation
        twice."""
        store.set(_path(f"{ENDPOINT}/AutoOffTime"), 30, etag=store.etag)
        assert not store.changed()

    def test_a_manual_edit_is_a_change(self, store: ConfigStore, directory: Path) -> None:
        (directory / "nodes.yaml").write_text(NODES.replace("LIGHT", "VALVE"), encoding="utf-8")
        assert store.changed()


class TestCheckAndSet:
    def test_a_stale_etag_is_refused(self, store: ConfigStore) -> None:
        """A silent last-write-wins would erase somebody else's change without a trace."""
        with pytest.raises(ConflictError):
            store.set(_path(f"{ENDPOINT}/AutoOffTime"), 30, etag="sha256:something-old")

    def test_a_refusal_does_not_touch_the_file(self, store: ConfigStore, directory: Path) -> None:
        before = (directory / "nodes.yaml").read_text(encoding="utf-8")
        with pytest.raises(ConflictError):
            store.set(_path(f"{ENDPOINT}/Purpose"), "VALVE", etag="sha256:stare")
        assert (directory / "nodes.yaml").read_text(encoding="utf-8") == before

    def test_somebody_elses_change_in_the_meantime_wins(
        self, store: ConfigStore, directory: Path
    ) -> None:
        old = store.etag
        (directory / "nodes.yaml").write_text(NODES.replace("LIGHT", "VALVE"), encoding="utf-8")
        with pytest.raises(ConflictError):
            store.set(_path(f"{ENDPOINT}/Purpose"), "PUMP", etag=old)
        assert "VALVE" in (directory / "nodes.yaml").read_text(encoding="utf-8")


class TestValidation:
    def test_invalid_content_leaves_no_trace(self, store: ConfigStore, directory: Path) -> None:
        """Step 1 is validation, step 2 is the file. In that order and not the other."""
        before = (directory / "nodes.yaml").read_text(encoding="utf-8")
        with pytest.raises(ConfigError):
            store.set(_path(f"{ENDPOINT}/Dishwasher"), 1, etag=store.etag)
        assert (directory / "nodes.yaml").read_text(encoding="utf-8") == before
        assert store.etag == ConfigStore(directory).etag, "the ETag did not move either"

    def test_state_is_not_configuration(self, store: ConfigStore) -> None:
        """`OnOff` is a node's state, not the operator's decision (KONFIGURACJA.md §7)."""
        with pytest.raises(ConfigError, match="is state"):
            store.set(_path(f"{ENDPOINT}/OnOff"), True, etag=store.etag)

    def test_validation_sees_the_whole_document_not_one_file(self, store: ConfigStore) -> None:
        """A binding in `bindings.yaml` refers to an alias from `nodes.yaml` — checking a
        file on its own would leave nothing to check against."""
        with pytest.raises(ConfigError, match="hall-switch"):
            store.set(
                ("bindings",),
                [
                    {
                        "from": {
                            "node": "hall-switch",
                            "endpoint": 4,
                            "type": "KeyInput",
                            "event": "Click",
                        },
                        "to": {
                            "node": "kitchen-relay",
                            "endpoint": 4,
                            "type": "Output",
                            "command": "Toggle",
                        },
                    }
                ],
                etag=store.etag,
            )

    def test_there_is_nothing_to_delete(self, store: ConfigStore) -> None:
        with pytest.raises(ConfigError, match="nothing to delete"):
            store.delete(_path("nodes/08090a0b0c0d0e0f/room"), etag=store.etag)


class TestFile:
    def test_comments_survive_a_write(self, store: ConfigStore, directory: Path) -> None:
        """A plain `yaml.dump()` erases comments — and after the first write from an
        application the file stops being what a file was for (§11.5)."""
        store.set(_path(f"{ENDPOINT}/AutoOffTime"), 30, etag=store.etag)
        content = (directory / "nodes.yaml").read_text(encoding="utf-8")
        assert "# The key is the Uid" in content
        assert "# a label for a human" in content
        assert "The lamp over the table" in content

    def test_key_order_survives_a_write(self, store: ConfigStore, directory: Path) -> None:
        store.set(_path(f"{ENDPOINT}/AutoOffTime"), 30, etag=store.etag)
        content = (directory / "nodes.yaml").read_text(encoding="utf-8")
        assert content.index("alias:") < content.index("name:") < content.index("endpoints:")

    def test_a_write_lands_in_the_right_file(self, store: ConfigStore, directory: Path) -> None:
        store.set(_path(f"{ENDPOINT}/AutoOffTime"), 30, etag=store.etag)
        assert "AutoOffTime" in (directory / "nodes.yaml").read_text(encoding="utf-8")
        assert "AutoOffTime" not in (directory / "hub.yaml").read_text(encoding="utf-8")

    def test_an_endpoint_number_lands_on_a_numeric_key(
        self, store: ConfigStore, directory: Path
    ) -> None:
        """In YAML `4` is a number, in a URL a string. Letting those drift apart would
        make a second endpoint `"4"` beside the first."""
        store.set(_path(f"{ENDPOINT}/AutoOffTime"), 30, etag=store.etag)
        content = (directory / "nodes.yaml").read_text(encoding="utf-8")
        assert '"4":' not in content and "'4':" not in content
        config = store.load()
        node = config.nodes[0]
        assert set(node.endpoints) == {4}

    def test_no_temporary_file_is_left_after_a_write(
        self, store: ConfigStore, directory: Path
    ) -> None:
        store.set(_path(f"{ENDPOINT}/AutoOffTime"), 30, etag=store.etag)
        assert list(directory.glob("*.tmp")) == []

    def test_deletion_works(self, store: ConfigStore, directory: Path) -> None:
        store.delete(_path(f"{ENDPOINT}/Purpose"), etag=store.etag)
        assert "Purpose" not in (directory / "nodes.yaml").read_text(encoding="utf-8")
        assert store.load().nodes[0].endpoints[4].attributes == {}


class TestFreshConfiguration:
    """Writing into an empty file differs from writing into a filled one — and differed
    wrongly.

    A key that is not there yet cannot be recognised from the map's contents: `4` landed
    as `'4'` as long as there was no `4` there. The same file then had two kinds of keys,
    differing by what somebody had written earlier.
    """

    @pytest.fixture
    def clean(self, tmp_path: Path) -> ConfigStore:
        from lagunhub.scaffold import init

        init(tmp_path, channel="vcan0")
        return ConfigStore(tmp_path)

    def test_the_first_endpoint_gets_a_numeric_key(self, clean: ConfigStore) -> None:
        clean.set_many(_with_type(30), etag=clean.etag)
        content = (clean.where / "nodes.yaml").read_text(encoding="utf-8")
        assert "\n      4:\n" in content, f"endpoint written as a string:\n{content}"

    def test_an_attribute_without_a_type_will_not_be_written(self, clean: ConfigStore) -> None:
        """That is why `set_many` exists: the attribute alone is invalid, and a write in
        two steps would leave the file correct only halfway."""
        with pytest.raises(ConfigError, match="has to give a `type`"):
            clean.set(_path(f"{ENDPOINT}/AutoOffTime"), 30, etag=clean.etag)

    def test_one_write_does_not_touch_two_files(self, clean: ConfigStore) -> None:
        with pytest.raises(ConfigError, match="two files"):
            clean.set_many(
                [(("groups", "5", "name"), "living-room"), (("bindings",), [])], etag=clean.etag
            )

    def test_the_first_group_gets_a_numeric_key(self, clean: ConfigStore) -> None:
        clean.set(("groups", "5", "name"), "living-room", etag=clean.etag)
        content = (clean.where / "hub.yaml").read_text(encoding="utf-8")
        assert "\n  5:\n" in content, f"group written as a string:\n{content}"

    def test_a_uid_stays_a_string_although_it_looks_like_a_number(self, clean: ConfigStore) -> None:
        """`0001020304050607` can be turned into a number — and never may be."""
        clean.set(_path("nodes/0001020304050607/alias"), "key", etag=clean.etag)
        config = clean.load()
        assert config.nodes[0].uid.hex() == "0001020304050607"
        assert "'0001020304050607':" in (clean.where / "nodes.yaml").read_text(encoding="utf-8")

    def test_comments_survive_the_first_write(self, clean: ConfigStore) -> None:
        """That is what `ruamel` round-trip mode is for: a fresh file is half comment,
        which explains what goes where."""
        clean.set_many(_with_type(30), etag=clean.etag)
        content = (clean.where / "nodes.yaml").read_text(encoding="utf-8")
        assert "# Nodes — the key is the Uid" in content
        assert "This file fills itself" in content

    def test_a_clean_configuration_loads_empty(self, clean: ConfigStore) -> None:
        config = clean.load()
        assert config.nodes == () and config.bindings == ()
        assert [b.channel for b in config.buses] == ["vcan0"]
        assert config.accept_new is False, "accepting is opened deliberately"


class TestSingleFile:
    def test_everything_goes_into_one_file(self, tmp_path: Path) -> None:
        file_ = tmp_path / "config.yaml"
        file_.write_text(HUB + NODES + BINDINGS, encoding="utf-8")
        store = ConfigStore(file_)
        store.set(_path(f"{ENDPOINT}/AutoOffTime"), 30, etag=store.etag)
        assert "AutoOffTime" in file_.read_text(encoding="utf-8")
        assert store.load().nodes[0].endpoints[4].attributes


class TestWatching:
    """An edit made in an editor is to be picked up without asking (§11.4).

    > **Bad configuration has no right to stop a working installation.**
    """

    def test_a_manual_change_is_picked_up(self, clock: Clock, directory: Path) -> None:
        inst, store = _hub(clock, directory)
        (directory / "nodes.yaml").write_text(NODES.replace("LIGHT", "VALVE"), encoding="utf-8")

        inst.run(1.5, step=0.25)

        assert inst.core.config_reloads == 1
        assert store.load().nodes[0].endpoints[4].attributes == {0x17: 2}, "VALVE"

    def test_a_broken_file_leaves_the_previous_configuration_in_force(
        self, clock: Clock, directory: Path
    ) -> None:
        inst, _ = _hub(clock, directory)
        before = dict(inst.installation.wanted)
        (directory / "nodes.yaml").write_text("nodes: {not-a-uid: {}}\n", encoding="utf-8")

        inst.run(1.5, step=0.25)

        assert inst.core.config_rejections == 1
        assert inst.core.config_reloads == 0
        assert inst.installation.wanted == before, "the old configuration stayed in force"
        assert any("was rejected" in issue for issue in inst.installation.issues)

    def test_the_same_issue_is_not_appended_every_second(
        self, clock: Clock, directory: Path
    ) -> None:
        """Without this, one write with a typo would bury the issue list."""
        inst, _ = _hub(clock, directory)
        (directory / "nodes.yaml").write_text("nodes: {not-a-uid: {}}\n", encoding="utf-8")

        inst.run(10.0, step=0.25)

        assert inst.core.config_rejections == 1
        assert len(inst.installation.issues) == 1

    def test_a_fixed_file_is_noticed_despite_the_earlier_error(
        self, clock: Clock, directory: Path
    ) -> None:
        inst, _ = _hub(clock, directory)
        (directory / "nodes.yaml").write_text("nodes: {not-a-uid: {}}\n", encoding="utf-8")
        inst.run(1.5, step=0.25)
        (directory / "nodes.yaml").write_text(NODES.replace("LIGHT", "PUMP"), encoding="utf-8")

        inst.run(1.5, step=0.25)

        assert inst.core.config_reloads == 1


def _hub(clock: Clock, directory: Path) -> tuple[Rig, ConfigStore]:
    store = ConfigStore(directory)
    inst = Rig(clock, config_store=store)
    inst.core.apply_config(store.load())
    return inst, store


def _with_type(value: int) -> list[tuple[tuple[str, ...], object]]:
    """The way `config_set_attribute` does it: the type and the attribute in one write."""
    return [
        (_path(f"{ENDPOINT}/type"), "Output"),
        (_path(f"{ENDPOINT}/AutoOffTime"), value),
    ]


def _path(text: str) -> tuple[str, ...]:
    return tuple(text.split("/"))
