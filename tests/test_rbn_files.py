"""rbn_files.py: RBN's patt3ch.lst and bcn.csv into rbn_known_dir; a bad fetch keeps the copy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import downloads
import rbn_files
import sparkgap

PATT3CH = b"+ K1@@\r\n  K1@@@@\r\n+ 8n6@@ \r\n"
BCN = b"28066,PY4ROE\r\n14100,4U1UN\r\n16000,YO5OHB-1\r\n"


@pytest.fixture(autouse=True)
def _few(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rbn_files, "MIN_PATTERNS", 2)
    monkeypatch.setattr(rbn_files, "MIN_BEACONS", 2)
    monkeypatch.setattr(rbn_files, "MIN_SCP_CALLS", 2)


def _entry(folder: Path, name: str) -> dict[str, object]:
    return json.loads((folder / downloads.MANIFEST).read_text())[name]


def test_patt3ch_is_saved_and_its_path_returned(tmp_path: Path) -> None:
    assert rbn_files.refresh_patt3ch(tmp_path, fetch=lambda: PATT3CH) == tmp_path / "patt3ch.lst"
    assert _entry(tmp_path, "patt3ch.lst")["records"] == 3


def test_the_beacon_list_is_saved_with_its_count(tmp_path: Path) -> None:
    assert rbn_files.refresh_beacons(tmp_path, fetch=lambda: BCN)
    assert (tmp_path / "bcn.csv").read_bytes() == BCN
    assert _entry(tmp_path, "bcn.csv")["records"] == 3


@pytest.mark.parametrize("bad", [b"", b"error code: 502", b"+ K1@@\r\n", OSError("502")])
def test_an_empty_bad_or_failed_fetch_keeps_the_copy(tmp_path: Path,
                                                     bad: bytes | Exception) -> None:
    rbn_files.refresh_patt3ch(tmp_path, fetch=lambda: PATT3CH)

    def get() -> bytes:
        if isinstance(bad, Exception):
            raise bad
        return bad

    got = rbn_files.refresh_patt3ch(tmp_path, fetch=get, now=lambda: 2e10)
    assert got == tmp_path / "patt3ch.lst"
    assert (tmp_path / "patt3ch.lst").read_bytes() == PATT3CH
    assert _entry(tmp_path, "patt3ch.lst")["error"]


def test_no_good_copy_falls_back_to_the_shipped_patt3ch(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "patt3ch.lst").write_bytes(b"")              # an Aggregator's empty download
    monkeypatch.setattr(rbn_files, "refresh_patt3ch", lambda d: None)
    assert sparkgap._fetch_rbn_files({"rbn_known_dir": str(tmp_path)}) == "patt3ch.lst"
    monkeypatch.setattr(rbn_files, "refresh_patt3ch", lambda d: d / "patt3ch.lst")
    got = sparkgap._fetch_rbn_files({"rbn_known_dir": str(tmp_path)})
    assert got == str(tmp_path / "patt3ch.lst")


def test_a_bad_copy_already_there_is_not_used(tmp_path: Path) -> None:
    (tmp_path / "patt3ch.lst").write_bytes(b"")

    def offline() -> bytes:
        raise OSError("502")

    assert rbn_files.refresh_patt3ch(tmp_path, fetch=offline) is None


def test_the_beacon_list_needs_the_gated_rule(tmp_path: Path, monkeypatch) -> None:
    got: list[Path] = []
    monkeypatch.setattr(rbn_files, "refresh_beacons", lambda d: got.append(d) or True)
    monkeypatch.setattr(rbn_files, "refresh_patt3ch", lambda d: None)
    sparkgap._fetch_rbn_files({"rbn_known_dir": str(tmp_path)})
    assert got == []
    sparkgap._fetch_rbn_files({"rbn_known_dir": str(tmp_path), "spot_rule_gates": True})
    assert got == [tmp_path]


SCP = b"!!Order,1,1\r\n#\r\n# Release 2026.11.18\r\nK1ABC\r\nVE2PID/W1\r\nW1AW\r\n"


def test_rbn_s_master_scp_is_saved_with_its_call_count(tmp_path: Path) -> None:
    assert rbn_files.refresh_master_scp(tmp_path, fetch=lambda: SCP) == tmp_path / "master.scp"
    assert _entry(tmp_path, "master.scp")["records"] == 3


@pytest.mark.parametrize("bad", [b"", b"<html>502</html>", b"#\r\nK1ABC\r\n"])
def test_a_bad_master_scp_keeps_the_copy(tmp_path: Path, bad: bytes) -> None:
    rbn_files.refresh_master_scp(tmp_path, fetch=lambda: SCP)
    got = rbn_files.refresh_master_scp(tmp_path, fetch=lambda: bad, now=lambda: 2e10)
    assert got == tmp_path / "master.scp" and (tmp_path / "master.scp").read_bytes() == SCP


def test_the_tracker_reads_rbn_s_master_scp_else_the_shipped_one(tmp_path: Path,
                                                                 monkeypatch) -> None:
    cfg = {"rbn_known_dir": str(tmp_path)}
    monkeypatch.setattr(rbn_files, "refresh_master_scp", lambda d: d / "master.scp")
    assert sparkgap._master_scp(cfg) == str(tmp_path / "master.scp")
    assert sparkgap._master_scp({**cfg, "master_scp": "MASTER.SCP"}) == str(tmp_path / "master.scp")
    assert sparkgap._master_scp({**cfg, "master_scp": "COMBINED.SCP"}) == "COMBINED.SCP"
    monkeypatch.setattr(rbn_files, "refresh_master_scp", lambda d: None)
    assert sparkgap._master_scp(cfg) == "MASTER.SCP"


def test_an_unusable_rbn_known_dir_falls_back_to_the_shipped_master_scp(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")
    cfg = {"rbn_known_dir": str(tmp_path / "file" / "rbn_known")}  # under a file: can't be made
    assert sparkgap._master_scp(cfg) == "MASTER.SCP"
