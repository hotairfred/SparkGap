"""Validation-weight refit (tools/eval/fit_validation.py), offline: logs and archive in memory."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "eval"))

from fit_validation import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    Check,
    Confirmer,
    episodes,
    parse_log,
)
from rbn_confirm import RbnSpot  # noqa: E402  # pyright: ignore[reportMissingImports]

UTC = timezone.utc
T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC).timestamp()
KEYS = ["bias", "single"]


def _raw(hms: str, f: float, text: str) -> str:
    return f"{hms} INFO ITILA raw {f:.1f} kHz cost=1.00: {text!r}\n"


def test_log_placed_in_utc_by_its_rbn_line() -> None:
    lines = [_raw("06:00:00", 7028.0, "CQ W9SN"),
             "06:00:05 INFO RBN: N0CALL-1,,,7028.0,40m,W9SN,,,CQ,11,2026-11-10 12:00:05,28,CW\n"]
    w = parse_log(lines)
    assert [(x.t, x.freq_khz, x.text) for x in w] == [(T0, 7028.0, "CQ W9SN")]


def test_log_rolls_over_local_midnight() -> None:
    lines = [_raw("23:59:50", 7028.0, "A"), _raw("00:00:10", 7028.0, "B")]
    w = parse_log(lines, start=datetime(2026, 11, 10, 5, 59, 50, tzinfo=UTC))
    assert w[1].t - w[0].t == 20


def test_log_without_offset_needs_start() -> None:
    with pytest.raises(ValueError):
        parse_log([_raw("06:00:00", 7028.0, "A")])


def _spot(spotter: str, dx: str, f: float, dt_s: float) -> RbnSpot:
    return RbnSpot(spotter, dx, f, datetime.fromtimestamp(T0 + dt_s, UTC))


def test_confirmed_by_another_skimmer_near_in_time_and_frequency() -> None:
    c = Confirmer([_spot("K5TR", "W9SN", 7028.4, 300)], "N0CALL-1")
    assert c.confirmed(T0, 7028.0, "W9SN")
    assert c.confirmed(T0, 7028.0, "W9SN/4")                # base call
    assert not c.confirmed(T0, 7026.9, "W9SN")              # over 1 kHz away
    assert not c.confirmed(T0 - 400, 7028.0, "W9SN")        # over 10 minutes
    assert not c.confirmed(T0, 7028.0, "W9SM")


def test_own_nodes_never_confirm() -> None:
    own = [_spot("N0CALL-5", "W9SN", 7028.0, 0), _spot("N0CALL/5", "W9SN", 7028.0, 0)]
    c = Confirmer(own, "N0CALL-1")
    assert not c.confirmed(T0, 7028.0, "W9SN")


def _check(t: float, f: float, call: str, single: float = 0.0) -> Check:
    return Check(T0 + t, f, call, {"bias": 1.0, "single": single})


def test_episodes_group_one_call_near_one_frequency() -> None:
    checks = [_check(0, 7028.0, "W9SN", 1.0), _check(60, 7028.3, "W9SN"),   # one episode
              _check(60, 7031.0, "W9SN"),                                     # another kHz
              _check(700, 7028.0, "W9SN")]                                    # gap over 10 minutes
    eps = episodes(checks, [False, True, False, False], KEYS)
    assert [(len(x), ok) for x, ok in eps] == [(2, True), (1, False), (1, False)]
    assert eps[0][0].tolist() == [[1.0, 1.0], [1.0, 0.0]]


def test_start_is_the_first_line_of_any_kind() -> None:
    lines = ["06:00:00 INFO Database: 50446 calls\n", _raw("06:00:30", 7028.0, "A")]
    w = parse_log(lines, start=datetime(2026, 11, 10, 12, 0, tzinfo=UTC))
    assert w[0].t == T0 + 30
