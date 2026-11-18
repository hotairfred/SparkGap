"""The RBN archive lines in a run log: what the node validation tool reads to score a dry run."""

import csv
import logging

import pytest

import sparkgap

Caplog = pytest.LogCaptureFixture
COLUMNS = ["callsign", "de_pfx", "de_cont", "freq", "band", "dx", "dx_pfx", "dx_cont",
           "mode", "db", "date", "speed", "tx_mode"]
T = 1_792_586_154.0  # 2026-10-21 12:35:54 UTC


def _lines(caplog: Caplog, prefix: str) -> list[str]:
    return [m[len(prefix):] for r in caplog.records if (m := r.getMessage()).startswith(prefix)]


def test_a_spot_is_one_archive_row(caplog: Caplog) -> None:
    with caplog.at_level(logging.INFO, logger="sparkgap"):
        sparkgap._log_rbn_row("n0call", 7027.64, "JA0FVU", 24.6, 30, T)
    (row,) = _lines(caplog, "RBN: ")
    got = dict(zip(COLUMNS, next(csv.reader([row]), []), strict=True))
    assert got == {"callsign": "N0CALL", "de_pfx": "", "de_cont": "", "freq": "7027.6",
                   "band": "40m", "dx": "JA0FVU", "dx_pfx": "", "dx_cont": "", "mode": "CQ",
                   "db": "25",
                   "date": "2026-10-21 12:35:54", "speed": "30", "tx_mode": "CW"}


def test_an_rtty_spot_is_a_row_with_its_baud(caplog: Caplog) -> None:
    with caplog.at_level(logging.INFO, logger="sparkgap"):
        sparkgap._log_rbn_row("n0call", 14083.0, "K1ABC", 26, sparkgap.RTTY_SPOT_BAUD, T, "RTTY")
    (row,) = _lines(caplog, "RBN: ")
    got = dict(zip(COLUMNS, next(csv.reader([row]), []), strict=True))
    assert (got["band"], got["mode"], got["speed"], got["tx_mode"]) == ("20m", "CQ", "45", "RTTY")


@pytest.mark.parametrize("khz, call, beacons, mode", [
    (14100.0, "4U1UN", None, "NCDXF B"),               # an NCDXF beacon on its frequency
    (7030.0, "4U1UN", None, "CQ"),                     # off it: not a beacon spot
    (28250.0, "K6RS/B", None, "BEACON"),
    (7039.0, "W5ABC", {"W5ABC": 7039.0}, "BEACON"),     # spot_rule_beacons
])
def test_a_beacon_spot_is_typed_as_rbn_stores_it(caplog: Caplog, khz: float, call: str,
                                                 beacons: dict[str, float] | None, mode: str) -> None:
    """The mode column RBN's archive shows, as the feeder types the spot (cdub89/SparkGap#49)."""
    with caplog.at_level(logging.INFO, logger="sparkgap"):
        sparkgap._log_rbn_row("n0call", khz, call, 12, 22, T, beacons=beacons)
    (row,) = _lines(caplog, "RBN: ")
    assert dict(zip(COLUMNS, next(csv.reader([row]), []), strict=True))["mode"] == mode


def test_the_spotter_keeps_a_node_suffix_but_not_the_default() -> None:
    assert sparkgap._rbn_spotter({"callsign": "WF8Z", "skimmer_suffix": "-1"}) == "WF8Z-1"
    assert sparkgap._rbn_spotter({"callsign": "WF8Z", "skimmer_suffix": "-#"}) == "WF8Z"
    assert sparkgap._rbn_spotter({"callsign": "WF8Z"}) == "WF8Z"
    assert sparkgap._rbn_spotter({"callsign": "WX7V-2", "skimmer_suffix": ""}) == "WX7V-2"


def test_a_span_line_holds_every_band(caplog: Caplog) -> None:
    with caplog.at_level(logging.INFO, logger="sparkgap"):
        sparkgap._log_rbn_span(T, [7038.4], 96_000)
        sparkgap._log_rbn_span(T + 60, [7038.4, 14038.4], 96_000)
    assert _lines(caplog, "RBN span: ") == ["2026-10-21 12:35:54 6990.4-7086.4",
                                            "2026-10-21 12:36:54 6990.4-7086.4 13990.4-14086.4"]


def test_the_node_line_carries_a_valid_grid_only(caplog: Caplog) -> None:
    with caplog.at_level(logging.INFO, logger="sparkgap"):
        sparkgap._log_rbn_node("n0call", "fn31PR")
        sparkgap._log_rbn_node("n0call", "FN31")
        sparkgap._log_rbn_node("n0call", "")
        sparkgap._log_rbn_node("n0call", "Shack")
    assert _lines(caplog, "RBN node: ") == ["N0CALL FN31pr", "N0CALL FN31", "N0CALL", "N0CALL"]
