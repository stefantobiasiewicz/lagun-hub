"""A Uid is an identity and an address is a route — and the types are to keep it that way.

The rule from [`07-hub.md`](../../lagunbus-spec/07-hub.md) §2 is enforced in the code by
the type system, not by discipline. This test checks that the enforcing really works: a
function taking a `Uid` will not take an address, and the other way round.

It is the only test that runs mypy, so it is also the only slow one in this directory.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HUB = Path(__file__).resolve().parent.parent

BAD_CODE = """
from lagunhub.core.model import Installation, NodeModel, Uid

inst = Installation()
node = NodeModel(uid=Uid(b"01234567"), address=0x02A, bus=0)
inst.add(node)

inst.nodes[0x02A]              # an address instead of a Uid
inst.readdress(0x02A, 0x040)   # the same the other way round
Uid(0x02A)                     # a number instead of bytes
"""

GOOD_CODE = """
from lagunhub.core.model import Installation, NodeModel, Uid

uid = Uid(b"01234567")
inst = Installation()
inst.add(NodeModel(uid=uid, address=0x02A, bus=0))
inst.nodes[uid]
inst.readdress(uid, 0x040)
"""


def check(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    file_ = tmp_path / "sample.py"
    file_.write_text(code, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--no-error-summary", str(file_)],
        cwd=HUB,
        capture_output=True,
        text=True,
    )


@pytest.mark.slow
def test_an_address_will_not_pass_where_a_uid_is_expected(tmp_path: Path) -> None:
    result = check(BAD_CODE, tmp_path)
    assert result.returncode != 0, "mypy let an address through where a Uid belongs"
    assert result.stdout.count("error:") >= 3, result.stdout


@pytest.mark.slow
def test_correct_use_passes(tmp_path: Path) -> None:
    result = check(GOOD_CODE, tmp_path)
    assert result.returncode == 0, result.stdout
