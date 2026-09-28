"""`lagun-hub doctor` — a check of the machine the hub is to stand on.

The tests are here so that **the advice is correct**, not merely so that something gets
printed. A human at a Raspberry Pi will read it once and do exactly what it says — so a
typo in a command costs an evening.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from lagunhub import doctor
from lagunhub.doctor import Level


class TestPluralForms:
    """The only place in the hub where a number goes straight into a sentence read by a
    human."""

    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (0, "0 nodes"),
            (1, "1 node"),
            (2, "2 nodes"),
            (112, "112 nodes"),
        ],
    )
    def test_the_noun_follows_the_number(self, count: int, expected: str) -> None:
        assert doctor.how_many(count, "node") == expected

    def test_an_irregular_plural_is_given_explicitly(self) -> None:
        assert doctor.how_many(2, "bus", "buses") == "2 buses"


class TestConfiguration:
    def test_a_missing_directory_says_what_to_do(self, tmp_path: Path) -> None:
        (result,) = [c for c in doctor.check(tmp_path / "no-such") if c.what == "configuration"]
        assert result.state is Level.ERROR
        assert "--config" in result.advice

    def test_broken_configuration_is_an_error_not_an_exception(self, tmp_path: Path) -> None:
        (tmp_path / "hub.yaml").write_text("nodes: {not-a-uid: {}}\n", encoding="utf-8")
        (result,) = [c for c in doctor.check(tmp_path) if c.what == "configuration"]
        assert result.state is Level.ERROR
        assert "will not start" in result.advice

    def test_a_valid_configuration_is_counted_up(self, tmp_path: Path) -> None:
        (tmp_path / "hub.yaml").write_text(_CONFIG, encoding="utf-8")
        (result,) = [c for c in doctor.check(tmp_path) if c.what == "configuration"]
        assert result.state is Level.OK
        assert result.detail == "1 node, 0 bindings"


class TestToken:
    def test_a_missing_token_is_not_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "hub.yaml").write_text(_CONFIG, encoding="utf-8")
        (result,) = [c for c in doctor.check(tmp_path) if c.what == "token"]
        assert result.state is Level.OK
        assert "will be drawn" in result.advice

    def test_too_wide_permissions_are_an_error_with_a_ready_command(self, tmp_path: Path) -> None:
        """A token file readable by everyone means everyone on the machine drives the
        house — so this is an error, not a warning."""
        (tmp_path / "hub.yaml").write_text(_CONFIG, encoding="utf-8")
        file_ = tmp_path / "api-token"
        file_.write_text("secret\n", encoding="utf-8")
        file_.chmod(0o644)

        (result,) = [c for c in doctor.check(tmp_path) if c.what == "token"]
        assert result.state is Level.ERROR
        assert result.advice.startswith(f"`chmod 600 {file_}`")

    def test_0600_permissions_pass(self, tmp_path: Path) -> None:
        (tmp_path / "hub.yaml").write_text(_CONFIG, encoding="utf-8")
        file_ = tmp_path / "api-token"
        file_.write_text("secret\n", encoding="utf-8")
        file_.chmod(0o600)

        (result,) = [c for c in doctor.check(tmp_path) if c.what == "token"]
        assert result.state is Level.OK
        assert oct(stat.S_IMODE(file_.stat().st_mode)) == "0o600"


class TestListening:
    def test_exposure_to_the_network_is_a_warning(self, tmp_path: Path) -> None:
        """Not an error — this is the operator's choice. But they are to know about it."""
        (tmp_path / "hub.yaml").write_text(
            _CONFIG + "api:\n  host: 0.0.0.0\n  port: 18080\n", encoding="utf-8"
        )
        (result,) = [c for c in doctor.check(tmp_path) if c.what.startswith("API")]
        assert result.state is Level.WARNING
        assert "ONLY protection" in result.advice

    def test_a_busy_port_says_where_to_look(self, tmp_path: Path) -> None:
        import socket

        with socket.socket() as occupying:
            occupying.bind(("127.0.0.1", 0))
            occupying.listen(1)
            port = occupying.getsockname()[1]
            (tmp_path / "hub.yaml").write_text(
                _CONFIG + f"api:\n  host: 127.0.0.1\n  port: {port}\n", encoding="utf-8"
            )
            (result,) = [c for c in doctor.check(tmp_path) if c.what.startswith("API")]
        assert result.state is Level.ERROR
        assert "systemctl status lagun-hub" in result.advice


class TestReport:
    def test_the_exit_code_says_whether_it_can_start(self, tmp_path: Path) -> None:
        """`doctor` makes sense in a script: zero means "start"."""
        (tmp_path / "hub.yaml").write_text(_CONFIG, encoding="utf-8")
        _, code = doctor.summary(doctor.check(tmp_path))
        assert code == 0

        _, code = doctor.summary(doctor.check(tmp_path / "no-such"))
        assert code == 1

    def test_every_piece_of_advice_ends_in_something_to_do(self, tmp_path: Path) -> None:
        """Advice that does not say WHAT TO DO is worse than none — it takes attention and
        gives nothing back."""
        for result in doctor.check(tmp_path / "no-such"):
            if result.advice:
                assert len(result.advice) > 20, result
                assert result.state is not Level.OK or "will be drawn" in result.advice

    def test_the_details_line_up_in_one_column(self, tmp_path: Path) -> None:
        """The report is read by eye from top to bottom — drifted columns force reading
        every line separately."""
        (tmp_path / "hub.yaml").write_text(_CONFIG, encoding="utf-8")
        results = doctor.check(tmp_path)
        lines = doctor.report(results).split("\n")

        columns = set()
        for result in results:
            if not result.detail:
                continue
            line = next(line for line in lines if result.what in line)
            columns.add(line.index(result.detail, line.index(result.what)))
        assert len(columns) == 1, f"details in different columns: {sorted(columns)}"


_CONFIG = """\
buses: [vcan0]
nodes:
  "0001020304050607":
    alias: key
"""
