"""RBN-archive confirmation (tools/eval/rbn_confirm.py), offline: archive rows built in memory."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "eval"))

from rbn_confirm import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    base_call,
    confirm,
    grid_latlon,
    miles,
    nearby_recall,
    nearby_spotters,
    parse_archive,
    parse_nodes,
    parse_spots,
)

UTC = timezone.utc  # datetime.UTC needs Python 3.11
T0 = datetime(2026, 9, 23, 19, 41, tzinfo=UTC)
T1 = datetime(2026, 9, 23, 19, 56, tzinfo=UTC)


def _row(spotter: str, dx: str, freq: str, date: str = "2026-09-23 19:45:00",
         mode: str = "CW") -> dict[str, str]:
    return {"callsign": spotter, "dx": dx, "freq": freq, "date": date, "tx_mode": mode}


def test_parses_file_mode_live_and_cluster_spot_lines() -> None:
    spots = parse_spots([
        "03:56:01 INFO SPOT: 14030.0 kHz K0TQ 20 dB 28 WPM [exact]",
        "03:56:02 INFO *** SPOT:    14041.2  K1AJ          17 dB  30 WPM  [exact] ***",
        "19:45:10 DX de K1XX-#:    14053.6  K7WP           21 dB  27 WPM   CQ     1945Z",
        "03:56:03 INFO ITILA raw 14030.0 kHz cost=0.10: 'CQ CWT K0TQ'",
    ])
    assert spots == {"K0TQ": {14030.0}, "K1AJ": {14041.2}, "K7WP": {14053.6}}


def test_base_call_strips_portable_designators() -> None:
    assert base_call("W1AW/4") == "W1AW"
    assert base_call("EA8/DL1ABC") == "DL1ABC"
    assert base_call("k0tq") == "K0TQ"


def test_confirmed_by_another_skimmer_within_tolerance_and_window() -> None:
    archive = list(parse_archive([
        _row("W3LPL", "K0TQ", "14030.6"),
        _row("VE6WZ", "K1AJ", "14043.5"),                           # 2.3 kHz away
        _row("KM3T", "K7WP", "14053.6", date="2026-09-23 21:00:00"),  # outside the window
        _row("K9LC", "N2PX", "14033.6", mode="RTTY"),               # not CW
    ]))
    heard = confirm({"K0TQ": {14030.0}, "K1AJ": {14041.2}, "K7WP": {14053.6}, "N2PX": {14033.6}},
                    archive, T0, T1)
    assert heard == {"K0TQ": {"W3LPL"}, "K1AJ": set(), "K7WP": set(), "N2PX": set()}


def test_own_node_is_excluded() -> None:
    archive = list(parse_archive([_row("K1XX/5", "K0TQ", "14030.0"),
                                  _row("WF8Z-2", "K0TQ", "14030.0")]))
    heard = confirm({"K0TQ": {14030.0}}, archive, T0, T1, exclude=("K1XX", "WF8Z"))
    assert heard == {"K0TQ": set()}


_NODES_HTML = """
<tr class="online"><td><a href="/dxsd1.php?f=0&c=W8WWV&t=de" title="x"> W8WWV </a></td>
  <td class="right"></td><td>EN91HM</td><td>K</td></tr>
<tr class="online"><td><a href="/dxsd1.php?f=0&c=KM3T&t=de" title="x"> KM3T </a></td>
  <td>FN42ET</td></tr>
<tr class="online"><td><a href="/dxsd1.php?f=0&c=N6TV&t=de" title="x"> N6TV </a></td>
  <td>CM97CF</td></tr>
<tr><td><a href="/dxsd1.php?f=0&c=NOGRID&t=de">NOGRID</a></td><td></td></tr>
"""


def test_parse_nodes_reads_callsign_and_grid() -> None:
    assert parse_nodes(_NODES_HTML) == {"W8WWV": "EN91HM", "KM3T": "FN42ET", "N6TV": "CM97CF"}


def test_grid_distance() -> None:
    home = grid_latlon("EM79SM")
    assert home is not None and grid_latlon("ZZ00") is None
    assert 205 < miles(home, grid_latlon("EN91HM")) < 220     # W8WWV, ~213 mi
    assert miles(home, grid_latlon("CM97CF")) > 1900          # N6TV, California


def test_nearby_spotters_radius_and_node_suffix_fallback() -> None:
    archive = list(parse_archive([_row("W8WWV", "K0TQ", "14030.0"),
                                  _row("KM3T-5", "K0TQ", "14030.0"),   # falls back to KM3T
                                  _row("N6TV", "K0TQ", "14030.0"),
                                  _row("ZZ9ZZ", "K0TQ", "14030.0")]))  # not in the node list
    nodes = parse_nodes(_NODES_HTML)
    assert set(nearby_spotters(archive, nodes, "EM79SM", 300)) == {"W8WWV"}
    assert set(nearby_spotters(archive, nodes, "EM79SM", 800)) == {"W8WWV", "KM3T-5"}  # FN42, ~704 mi


def test_confirm_only_counts_listed_spotters() -> None:
    archive = list(parse_archive([_row("W8WWV", "K0TQ", "14030.0"),
                                  _row("N6TV", "K1AJ", "14041.0")]))
    heard = confirm({"K0TQ": {14030.0}, "K1AJ": {14041.0}}, archive, T0, T1, only={"W8WWV"})
    assert heard == {"K0TQ": {"W8WWV"}, "K1AJ": set()}


def test_nearby_recall_bands_window_and_min_skimmers() -> None:
    archive = list(parse_archive([
        _row("W8WWV", "K0TQ", "14030.0"), _row("WT9U", "K0TQ", "14030.1"),
        _row("W8WWV", "K1AJ", "14041.0"),
        _row("W8WWV", "N2PX", "7030.0"),                                 # band we don't cover
        _row("W8WWV", "K7WP", "14053.6", date="2026-09-23 21:00:00"),   # outside the window
        _row("N6TV", "W1AW", "14050.0"),                                 # not nearby
    ]))
    near = {"W8WWV", "WT9U"}
    rec = nearby_recall({"K0TQ": {14030.4}}, archive, near, T0, T1, {"20m"})
    assert rec == {"K0TQ": ({"W8WWV", "WT9U"}, True), "K1AJ": ({"W8WWV"}, False)}
    rec2 = nearby_recall({"K0TQ": {14030.4}}, archive, near, T0, T1, {"20m"}, min_nearby=2)
    assert rec2 == {"K0TQ": ({"W8WWV", "WT9U"}, True)}
