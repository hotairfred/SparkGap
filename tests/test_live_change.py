"""SparkGap._live_change: a band change from the GUI feed without a restart (Flex, one band)."""

import asyncio
from typing import Any

import numpy as np
import pytest

from sparkgap import SparkGap

RATE = 96_000


class Radio:
    def __init__(self, events: list[str], fail: bool = False) -> None:
        self.events, self.fail = events, fail

    def retune(self, freq_hz: int) -> None:
        self.events.append(f"retune {freq_hz}")
        if self.fail:
            raise RuntimeError("[Flex] The radio refused 14.038400 MHz")


class Manager:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self._wpm_cache = {100: 25}
        self._pileup_history = {7021.0: {}}
        self._spectrum_tap = None

    def kill_all(self) -> None:
        self.events.append("kill_all")

    def update_signals(self, signals: list[Any], center_khz: float) -> None:
        self.events.append(f"scanner {center_khz:.1f}")


class Telnet:
    bands: list[tuple[float, float]] = []


def _copy(events: list[str], fail: bool = False, **cfg: Any) -> Any:
    sg: Any = SparkGap({"sdr_type": "flex", "sample_rate": RATE, "bands": ["40m"], **cfg})
    name, center = sg._resolve_band("40m", RATE)
    sg._band_meta = [(name, center, 0)]
    sg._band_bufs = {0: {"iq": np.ones((4, 2)), "raw": [[1.0]], "i": [1], "q": [1]}}
    sg.managers = [Manager(events)]
    sg.receiver = Radio(events, fail)
    sg.telnet = Telnet()
    sg._wav_record = None
    return sg


def _change(sg: Any, change: dict[str, Any]) -> tuple[bool, str]:
    out: list[tuple[bool, str]] = []
    asyncio.run(sg._live_change(change, lambda ok, text: out.append((ok, text))))
    return out[0]


def test_the_radio_moves_first_then_the_old_band_is_dropped() -> None:
    events: list[str] = []
    sg = _copy(events)
    _, center = sg._resolve_band("20m", RATE)
    assert _change(sg, {"band": "20m"}) == (True, "retuned to 20m")
    assert events == [f"retune {center}", "kill_all", f"scanner {center / 1000:.1f}"]
    assert sg._band_meta == [("20m", center, 0)] and sg.cfg["bands"] == ["20m"]
    buf = sg._band_bufs[0]
    assert buf["raw"] == [] and len(buf["iq"]) == 0
    mgr = sg.managers[0]
    assert isinstance(mgr, Manager) and not mgr._wpm_cache and not mgr._pileup_history
    lo, hi = sg.telnet.bands[0]
    assert lo < center / 1000 < hi


def test_a_refused_retune_changes_nothing() -> None:
    events: list[str] = []
    sg = _copy(events, fail=True)
    ok, text = _change(sg, {"band": "20m"})
    assert not ok and "refused" in text
    assert len(events) == 1 and events[0].startswith("retune")
    assert sg._band_meta[0][0] == "40m" and sg.cfg["bands"] == ["40m"]
    assert len(sg._band_bufs[0]["iq"]) == 4


def test_the_same_band_is_already_done() -> None:
    events: list[str] = []
    assert _change(_copy(events), {"band": "40m"}) == (True, "already on 40m") and not events


@pytest.mark.parametrize(
    "change, cfg, why",
    [
        ({"band": "20m", "x": 1}, {}, "only the band"),
        ({"band": "6m"}, {}, "unknown band"),
        ({"band": []}, {}, "unknown band"),
        ({"band": "20m"}, {"per_band_min_snr": {"20m": 9}}, "per-band settings"),
        ({"band": "20m"}, {"pfb_scanner_bands": ["20m"]}, "per-band settings"),
    ],
)
def test_what_cannot_change_live_is_refused_untouched(
    change: dict[str, Any], cfg: dict[str, Any], why: str
) -> None:
    events: list[str] = []
    ok, text = _change(_copy(events, **cfg), change)
    assert not ok and why in text and not events


def test_recording_or_two_bands_is_refused() -> None:
    events: list[str] = []
    sg = _copy(events)
    sg._wav_record = object()
    assert "recording" in _change(sg, {"band": "20m"})[1]
    sg = _copy(events)
    sg._band_meta.append(("20m", 14_038_400, 1))
    assert "more than one band" in _change(sg, {"band": "20m"})[1]
    assert not events


def test_a_second_change_while_one_is_under_way_is_busy() -> None:
    sg = _copy([])
    out: list[tuple[bool, str]] = []
    sg._take_control({"band": "20m"}, lambda ok, text: out.append((ok, text)))
    sg._take_control({"band": "17m"}, lambda ok, text: out.append((ok, text)))
    assert len(sg._controls) == 1 and out == [(False, "busy: a change is under way")]
