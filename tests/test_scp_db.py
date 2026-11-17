"""Super Check Partial calls (scp_db.py): reading SCP.DB, daily refresh, offline cache, wiring."""

import sqlite3
from pathlib import Path

import pytest

import scp_db
import sparkgap


def _db(path: Path, calls: list[str]) -> bytes:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE callsigns (callsign TEXT PRIMARY KEY, annual_rate INTEGER NOT NULL)")
    con.executemany("INSERT INTO callsigns VALUES (?, 1)", [(c,) for c in calls])
    con.commit()
    con.close()
    return path.read_bytes()


def test_calls_reads_the_callsigns_table(tmp_path: Path) -> None:
    _db(tmp_path / "x.db", ["K1ABC", "fr/g3txf"])
    assert scp_db.calls(tmp_path / "x.db") == {"K1ABC", "FR/G3TXF"}


def test_refresh_daily_and_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scp_db, "MIN_CALLS", 1)
    data = _db(tmp_path / "src.db", ["K1ABC", "W1AW"])
    cache = tmp_path / "cache"
    fetches: list[int] = []

    def fetch() -> bytes:
        fetches.append(1)
        return data

    assert scp_db.refresh(cache, fetch=fetch) == {"K1ABC", "W1AW"}
    assert scp_db.refresh(cache, fetch=fetch) == {"K1ABC", "W1AW"}
    assert len(fetches) == 1                       # fresh cache: no second download

    def offline() -> bytes:
        raise OSError("network unreachable")

    assert scp_db.refresh(cache, fetch=offline, now=lambda: 2e10) == {"K1ABC", "W1AW"}
    page = b"<html>not found</html>"
    assert scp_db.refresh(cache, fetch=lambda: page, now=lambda: 2e10) == {"K1ABC", "W1AW"}
    tiny = _db(tmp_path / "tiny.db", ["K1ABC"])                             # fewer than MIN_CALLS
    monkeypatch.setattr(scp_db, "MIN_CALLS", 2)
    assert scp_db.refresh(cache, fetch=lambda: tiny, now=lambda: 2e10) == {"K1ABC", "W1AW"}
    assert sorted(p.name for p in cache.iterdir()) == ["SCP.DB"]     # no temp file left behind


def test_refresh_without_download_or_cache_is_empty(tmp_path: Path) -> None:
    def offline() -> bytes:
        raise OSError("network unreachable")

    assert scp_db.refresh(tmp_path, fetch=offline) == frozenset()


def _tracker():
    return type("T", (), {"_matches_patt3ch": lambda self, c: "active"})()


def test_spot_rule_scp_reads_the_cache(tmp_path: Path) -> None:
    _db(tmp_path / "SCP.DB", ["K1ABC"])                       # fresh: read, not fetched
    cfg = {"spot_rule_gates": True, "spot_rule_scp": True, "rbn_known_dir": str(tmp_path)}
    rule = sparkgap._make_spot_rule(cfg, _tracker())
    assert rule.scp is not None and rule.scp("K1ABC") and not rule.scp("K1ABD")
    assert sparkgap._make_spot_rule(dict(cfg, spot_rule_scp=False), _tracker()).scp is None


def test_spot_rule_scp_without_scp_db_is_off(tmp_path: Path, monkeypatch) -> None:
    def offline() -> bytes:
        raise OSError("network unreachable")

    monkeypatch.setattr(scp_db, "_fetch", offline)
    cfg = {"spot_rule_gates": True, "spot_rule_scp": True, "rbn_known_dir": str(tmp_path)}
    assert sparkgap._make_spot_rule(cfg, _tracker()).scp is None


def test_spot_rule_scp_without_the_gate_rule_says_so(caplog: pytest.LogCaptureFixture) -> None:
    import logging
    caplog.set_level(logging.WARNING, logger="sparkgap")
    sparkgap._make_spot_rule({"spot_rule": "repeat", "spot_rule_scp": True}, _tracker())
    assert "spot_rule_scp is on but spot_rule_gates is off" in caplog.text
