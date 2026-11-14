"""enable_ft4: each band's FT4 dial is decoded with the FT8 minute (decode_ft8 -ft4, 7.5 s slots); FT4
decodes are spots of mode FT4 at the FT4 dial; FT8 is unchanged and off by default nothing changes."""

import types

import numpy as np
import pytest

import sparkgap

RATE, N = 12000, 60 * 12000
TX_S = 4.48                                    # an FT4 transmission: 105 symbols of 0.048 s


def test_ft4_dials_in_the_span() -> None:
    assert sparkgap.ft4_dial_khz(14090) == 14080
    assert sparkgap.ft4_dial_khz(21090) == 21140
    assert sparkgap.ft4_dial_khz(7090) == 7047.5
    assert sparkgap.ft4_dial_khz(28090) == 28180
    assert sparkgap.ft4_dial_khz(1886.8) is None            # 160m has none
    assert sparkgap.ft4_dial_khz(14040, fs=48000) is None   # 14080 outside a 48 kHz span


@pytest.mark.parametrize("frac", [0.0, 0.25, 3.0, 7.4, 11.0])
def test_every_whole_ft4_transmission_is_inside_a_window(frac: float) -> None:
    t0 = 1792551300.0 + frac
    starts = [k / RATE for k in sparkgap.ft8_window_starts(t0, N, slot_s=7.5)]
    assert all(s * RATE + 7.5 * RATE <= N for s in starts)
    slot = (-t0) % 7.5 - 7.5
    while slot < 60:
        tx0 = slot + 0.5
        if tx0 >= 0 and tx0 + TX_S <= 60:
            assert any(s <= tx0 and s + 7.5 >= tx0 + TX_S for s in starts), (frac, slot)
        slot += 7.5


def test_ft8_windows_unchanged() -> None:
    t0 = 1792551360.25
    assert sparkgap.ft8_window_starts(t0, N) == sparkgap.ft8_window_starts(t0, N, slot_s=15.0)


def _cycle(monkeypatch, ft4: bool):
    runs, spots = [], []

    def fake_run(args, **kw):
        runs.append(args)
        msg = "CQ K9EAG EM34" if "-ft4" in args else "CQ N4DWD EM86"
        return types.SimpleNamespace(stdout=f"000000 +05.0 +0.50 1500 ~  {msg}\n")

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sparkgap, "_get_mqtt_publisher", lambda: None)
    sk = types.SimpleNamespace(cfg={"enable_ft8": True, "enable_ft4": ft4, "enable_rtty": False},
                               spot_count=0, managers=[], _corrected_freq_khz=lambda f: f,
                               broadcast=lambda **s: spots.append(s))
    z = np.zeros(60 * 192000, dtype=np.float32)
    sparkgap._ft8_cycle([("20m", 0, 14090.0, 14074, z, z, len(z), 1792551360.0)], sk)
    return runs, spots


def test_ft4_spots_at_the_ft4_dial(monkeypatch) -> None:
    runs, spots = _cycle(monkeypatch, True)
    ft4 = [a for a in runs if "-ft4" in a]
    assert len(ft4) == len(sparkgap.ft8_window_starts(1792551360.0, N, slot_s=7.5))
    assert len(runs) - len(ft4) == len(sparkgap.ft8_window_starts(1792551360.0, N))
    by_mode = {s["mode"]: s for s in spots}
    assert set(by_mode) == {"FT8", "FT4"} and len(spots) == 2   # one spot each: repeats deduped
    assert by_mode["FT4"]["freq_khz"] == pytest.approx(14081.5) and by_mode["FT4"]["dx_call"] == "K9EAG"
    assert by_mode["FT8"]["freq_khz"] == pytest.approx(14075.5) and by_mode["FT8"]["dx_call"] == "N4DWD"


def test_off_by_default_nothing_changes(monkeypatch) -> None:
    runs, spots = _cycle(monkeypatch, False)
    assert not any("-ft4" in a for a in runs)
    assert [s["mode"] for s in spots] == ["FT8"]
    assert sparkgap._gui_settings({}, 192000)[0]["enable_ft4"] is False
