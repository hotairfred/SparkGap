"""sparkgap.sh setup's node settings: asked with the current values as defaults, written to the
radio file and copied to that radio's receivers; a rerun changes only what the answers change."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

import radios
from tools import setup_node

RADIO = {"type": "flex", "nickname": "Lake", "model": "FLEX-6600", "address": "192.0.2.101"}


def answers(*lines: str) -> Callable[[str], str]:
    it = iter(lines)
    return lambda _prompt: next(it)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "Lake.radio.json").write_text(json.dumps(RADIO))
    for name, radio in (("Lake-3", "Lake"), ("Lake-4", "Lake"), ("Dallas-1", "Dallas")):
        (tmp_path / "config" / f"{name}.local.json").write_text(
            json.dumps({"radio": radio, "callsign": "N0CALL", "grid": "AA00aa", "bands": ["20m"]}))
    return tmp_path


def cfg(root: Path, name: str) -> dict[str, object]:
    return json.loads((root / "config" / name).read_text())


def test_a_new_node_is_written_and_its_receivers_read_it(root: Path) -> None:
    setup_node.setup(root, "Lake", answers("n0call-2", "em12UB", "Op", ""))
    st = cfg(root, "Lake.radio.json")
    assert (st["callsign"], st["grid"], st["op_name"], st["rbn_upload"]) == (
        "N0CALL-2", "EM12ub", "Op", "dry")
    assert st["address"] == "192.0.2.101"
    for name in ("Lake-3", "Lake-4"):
        c = cfg(root, f"{name}.local.json")
        assert "callsign" not in c and "grid" not in c  # read from the radio file
    assert cfg(root, "Dallas-1.local.json")["callsign"] == "N0CALL"  # another radio's receiver
    assert len(list((root / "config").glob("Lake-3.local.json.pre-setup-*"))) == 1


def test_a_rerun_keeps_every_value_on_enter(root: Path) -> None:
    setup_node.setup(root, "Lake", answers("N0CALL-2", "EM12ub", "Op", "y"))
    before = sorted(p.name for p in (root / "config").iterdir())
    assert setup_node.setup(root, "Lake", answers("", "", "")) == ["Lake: unchanged"]
    assert sorted(p.name for p in (root / "config").iterdir()) == before  # no new backups


def test_a_live_upload_is_left_as_it_is(root: Path) -> None:
    (root / "config" / "Lake.radio.json").write_text(json.dumps({**RADIO, "rbn_upload": "live"}))
    setup_node.setup(root, "Lake", answers("N0CALL-2", "EM12ub", "Op", ""))
    assert cfg(root, "Lake.radio.json")["rbn_upload"] == "live"


def test_bad_answers_are_asked_again(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup_node.setup(root, "Lake", answers("not a call", "N0CALL-2", "EM1", "EM12", "", "Op", ""))
    st = cfg(root, "Lake.radio.json")
    assert (st["callsign"], st["grid"], st["op_name"]) == ("N0CALL-2", "EM12", "Op")
    out = capsys.readouterr().out
    assert "a callsign" in out and "a 4 or 6 character grid" in out and "operator's name" in out


@pytest.mark.parametrize("call", ["N0CALL", "N0CALL-2", "N0CALL/5", "VE3ABC-12"])
def test_callsigns_accepted(call: str) -> None:
    assert radios.check_call(call) is None


@pytest.mark.parametrize("call", ["", "N0CALL-123", "CALL", "N0 CALL", "N0CALL-"])
def test_callsigns_refused(call: str) -> None:
    assert radios.check_call(call) is not None


def test_no_answer_changes_nothing(root: Path) -> None:
    def eof(_prompt: str) -> str:
        raise EOFError

    with pytest.raises(EOFError):
        setup_node.setup(root, "Lake", eof)
    assert cfg(root, "Lake.radio.json") == RADIO


def test_no_to_save_writes_nothing(root: Path) -> None:
    before = {p.name: p.read_text() for p in (root / "config").iterdir()}
    out = setup_node.setup(root, "Lake", answers("N0CALL-2", "EM12ub", "Op", "n"))
    assert out == ["nothing changed"]
    assert {p.name: p.read_text() for p in (root / "config").iterdir()} == before


def test_running_receivers_are_named_in_the_question_and_returned(root: Path) -> None:
    import os

    (root / "logs").mkdir()
    (root / "logs" / "Lake-3.pid").write_text(str(os.getpid()))  # Lake-3 runs, Lake-4 does not
    asked: list[str] = []
    replies = iter(["N0CALL-2", "EM12ub", "Op", "y"])

    def read(prompt: str) -> str:
        asked.append(prompt)
        return next(replies)

    out = setup_node.setup(root, "Lake", read)
    assert asked[-1] == "Save and restart Lake-3? [Y/n] "
    assert out[-1] == "restart: Lake-3"
