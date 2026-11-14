"""ftx_decoder "jt9": WSJT-X's jt9 decodes the same FT8/FT4 slices as decode_ft8, one window per slot,
at jt9_depth; its a-priori marks are dropped from the message and its DT goes to the clock watch as is."""

import types

import numpy as np
import pytest

import sparkgap

T0 = 1792551360.0                      # a minute boundary


def test_off_by_default() -> None:
    assert sparkgap._jt9_decoder({}) is None
    assert sparkgap._jt9_decoder({"ftx_decoder": "ft8_lib"}) is None
    eff = sparkgap._gui_settings({}, 192000)[0]
    assert eff["ftx_decoder"] == "ft8_lib" and eff["jt9_depth"] is None


def test_depth_and_path() -> None:
    prog, depth, work = sparkgap._jt9_decoder({"ftx_decoder": "jt9", "jt9_depth": 7, "jt9_path": "/opt/jt9"})
    assert (prog, depth) == ("/opt/jt9", 3)
    assert sparkgap._jt9_decoder({"ftx_decoder": "JT9", "jt9_depth": 0})[1] == 1
    assert sparkgap._jt9_args(("jt9", 2, work), "FT8") == ["-8", "-p", "15", "-d", "2", "-a", work, "-t", work]
    assert sparkgap._jt9_args(("jt9", 2, work), "FT4")[:3] == ["-5", "-p", "7"]


def _cycle(monkeypatch, cfg):
    runs, spots, dts = [], [], []

    def fake_run(args, **kw):
        runs.append(args)
        line = ("000000 -12  0.3 1500 ~  CQ K9EAG EM34                     a2" if "-5" in args
                else "000000  -5  0.2 1500 ~  CQ N4DWD EM86")
        return types.SimpleNamespace(stdout=line + "\n<DecodeFinished>   0   1        0\n")

    class Watch:
        def add(self, d, now):
            dts.extend(d)
            return None, None

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sparkgap, "_get_mqtt_publisher", lambda: None)
    monkeypatch.setattr(sparkgap, "Ft8ClockWatch", Watch)
    sk = types.SimpleNamespace(cfg={"enable_ft8": True, "enable_rtty": False, **cfg}, spot_count=0,
                               managers=[], _corrected_freq_khz=lambda f: f,
                               broadcast=lambda **s: spots.append(s))
    z = np.zeros(60 * 192000, dtype=np.float32)
    sparkgap._ft8_cycle([("20m", 0, 14090.0, 14074, z, z, len(z), T0)], sk)
    return runs, spots, dts


def test_jt9_one_window_per_slot(monkeypatch) -> None:
    runs, spots, dts = _cycle(monkeypatch, {"ftx_decoder": "jt9", "jt9_path": "/opt/jt9", "jt9_depth": 2,
                                            "enable_ft4": True})
    ft8 = [a for a in runs if "-8" in a]
    ft4 = [a for a in runs if "-5" in a]
    assert all(a[0] == "/opt/jt9" and a[a.index("-d") + 1] == "2" for a in runs)
    assert len(ft8) == 4 and len(ft4) == 8             # one window per 15 s and per 7.5 s slot
    by_mode = {s["mode"]: s for s in spots}
    assert by_mode["FT4"]["dx_call"] == "K9EAG" and by_mode["FT4"]["comment"] == "CQ K9EAG EM34"   # a2 dropped
    assert by_mode["FT8"]["freq_khz"] == pytest.approx(14075.5)
    assert dts == [pytest.approx(0.2)]                 # jt9's DT as WSJT-X shows it (one FT8 message, deduped)


def test_decode_ft8_path_unchanged(monkeypatch) -> None:
    runs, _spots, _ = _cycle(monkeypatch, {})
    assert all("-d" not in a for a in runs)
    assert len(runs) == len(sparkgap.ft8_window_starts(T0, 60 * 12000))
