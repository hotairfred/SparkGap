"""RBN known calls (rbn_known.py): counts per day, own spotter left out, cache, missing days."""

import io
import urllib.error
import zipfile
from datetime import date
from email.message import Message
from pathlib import Path

from rbn_known import base_call, day_counts, known_calls, refresh, spotter

HEADER = "callsign,de_pfx,de_cont,freq,band,dx,dx_pfx,dx_cont,mode,db,date,speed,tx_mode\n"


def _nap(seconds: float) -> None:
    del seconds


def _row(spotter: str, dx: str, mode: str = "CW") -> str:
    return f"{spotter},K,NA,7030.0,40m,{dx},K,NA,CQ,20,2026-10-13 00:00:00,20,{mode}\n"


def _zip(rows: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("20261013.csv", HEADER + "".join(rows))
    return buf.getvalue()


def test_base_call_is_the_operator_call() -> None:
    assert base_call("PJ2/AG3I") == "AG3I"
    assert base_call("ag3i/p") == "AG3I"
    assert base_call("K1ABC") == "K1ABC"


def test_day_counts_distinct_skimmers_without_our_own() -> None:
    import csv
    rows = csv.DictReader(io.StringIO(HEADER + _row("W2NNN", "K1ABC") + _row("W2NNN", "K1ABC")
                                      + _row("VE3EID", "K1ABC/P") + _row("WX7V/5", "K1ABC")
                                      + _row("WX7V/5", "WA8VZ") + _row("W2NNN", "K1RT", "RTTY")))
    assert day_counts(rows, "WX7V") == {"K1ABC": 2}


def test_spotter_is_the_operator_behind_a_node() -> None:
    assert spotter("WF8Z-2") == spotter("WF8Z-#") == spotter("WF8Z") == "WF8Z"
    assert spotter("WX7V/5") == "WX7V"


def test_day_counts_own_nodes_left_out_and_one_operators_nodes_once() -> None:
    import csv
    rows = csv.DictReader(io.StringIO(HEADER + _row("WF8Z-2", "OK2PDE") + _row("KM3T-2", "K1ABC")
                                      + _row("KM3T-5", "K1ABC") + _row("W1NT-6", "K1ABC")))
    assert day_counts(rows, "WF8Z") == {"K1ABC": 2}


def test_known_calls_need_enough_skimmers_on_one_day() -> None:
    from collections import Counter
    assert known_calls([Counter({"K1ABC": 10, "W1AW": 9}), Counter({"W1AW": 9})]) == {"K1ABC"}


def test_refresh_caches_days_and_skips_unpublished(tmp_path: Path) -> None:
    fetched: list[date] = []

    def fetch(day: date) -> bytes:
        fetched.append(day)
        if day == date(2026, 10, 13):
            raise urllib.error.HTTPError("u", 404, "Not Found", Message(), None)
        return _zip([_row(f"SK{i}", "K1ABC") for i in range(10)] + [_row("SK0", "W1AW")])

    assert refresh(tmp_path, "WX7V", today=date(2026, 10, 14), fetch=fetch,
                   sleep=_nap) == {"K1ABC"}
    assert len(fetched) == 7
    assert refresh(tmp_path, "WX7V", today=date(2026, 10, 14), fetch=fetch,
                   sleep=_nap) == {"K1ABC"}
    assert fetched[7:] == [date(2026, 10, 13)]           # only the missing day is tried again
    assert (tmp_path / "rbn_known.txt").read_text().splitlines()[1:] == ["K1ABC"]


def test_refresh_refetches_a_damaged_day_and_stops_when_offline(tmp_path: Path) -> None:
    good = _zip([_row(f"SK{i}", "K1ABC") for i in range(10)])
    refresh(tmp_path, "WX7V", today=date(2026, 10, 14), fetch=lambda day: good,
            sleep=_nap)
    (tmp_path / "20261010_WX7V_v2.calls").write_text("K1ABC\t1")      # cut off mid-line
    fetched: list[date] = []

    def offline(day: date) -> bytes:
        fetched.append(day)
        raise OSError("network unreachable")

    assert refresh(tmp_path, "WX7V", today=date(2026, 10, 15), fetch=offline,
                   sleep=_nap) == {"K1ABC"}
    assert fetched == [date(2026, 10, 14)]               # first failure: no more tries this start


def test_refresh_window_is_configurable_paced_and_kept(tmp_path: Path) -> None:
    pauses: list[float] = []
    good = _zip([_row(f"SK{i}", "K1ABC") for i in range(10)])
    refresh(tmp_path, "WX7V", days=40, today=date(2026, 10, 14), fetch=lambda day: good,
            sleep=pauses.append)
    assert len(list(tmp_path.glob("*.calls"))) == 40     # not pruned at KEEP_DAYS (30)
    assert len(pauses) == 39                             # between downloads, not before the first


def test_refresh_stops_when_rbn_refuses_and_skips_a_truncated_day(tmp_path: Path) -> None:
    import http.client
    fetched: list[date] = []

    def refusing(day: date) -> bytes:
        fetched.append(day)
        if day == date(2026, 10, 13):
            raise http.client.IncompleteRead(b"PK")
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", Message(), None)

    assert refresh(tmp_path, "WX7V", today=date(2026, 10, 14), fetch=refusing,
                   sleep=_nap) == set()
    assert fetched == [date(2026, 10, 13), date(2026, 10, 12)]   # stops at the refusal
