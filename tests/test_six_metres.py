"""6m: "6m" is its CW segment (50.000-50.100); a second receiver at SIX_M_DIGITAL_KHZ decodes FT8 50.313
and FT4 50.318 and has no CW in its span."""

import types

import numpy as np

import sparkgap
from sparkgap import (
    BANDS,
    SIX_M_DIGITAL_KHZ,
    SparkGap,
    _decode_span,
    _derive_cw_window,
    band_center_hz,
    ft4_dial_khz,
    ft8_dial_khz,
)


def test_6m_band_is_its_cw_segment() -> None:
    assert BANDS["6m"] == 50000
    assert band_center_hz("6m", 192_000) == 50_076_800
    assert SparkGap._resolve_band("6m", 192_000) == ("6m", 50_076_800)
    assert _derive_cw_window(50076.8) == (50000.0, 50100.0)
    assert _derive_cw_window(50076.8, whole_segment=True) == (50000.0, 50100.0)
    assert _decode_span(50000.0, 50100.0, 50076.8, 192_000) == (50000.0, 50100.0)
    assert ft8_dial_khz(50076.8) is None and ft4_dial_khz(50076.8) is None


def test_6m_digital_receiver() -> None:
    assert SparkGap._resolve_band({"name": "6m digital", "center_khz": SIX_M_DIGITAL_KHZ}, 192_000) == (
        "6m digital", 50_290_000)
    assert ft8_dial_khz(SIX_M_DIGITAL_KHZ) == 50313
    assert ft4_dial_khz(SIX_M_DIGITAL_KHZ) == 50318
    lo, hi = _decode_span(*_derive_cw_window(SIX_M_DIGITAL_KHZ), SIX_M_DIGITAL_KHZ, 192_000)
    assert hi <= lo                                            # no CW in this span
    half = 192 / 2 - 2
    assert sparkgap.wspr_dial_khz(SIX_M_DIGITAL_KHZ) == 50293.0
    for dial in (50293, 50260, 50380):                         # WSPR, MSK144 (both NA dials)
        assert SIX_M_DIGITAL_KHZ - half <= dial and dial + 3 <= SIX_M_DIGITAL_KHZ + half


def test_hf_bands_unchanged() -> None:
    assert ft8_dial_khz(14076.8) == 14074 and ft4_dial_khz(14090) == 14080
    assert _derive_cw_window(28076.8) == (28000.0, 28070.0)


def test_6m_digital_cycle_spots_ft8_and_ft4(monkeypatch) -> None:
    spots = []

    def fake_run(args, **kw):
        msg = "CQ K9EAG EM34" if "-ft4" in args else "CQ W5LDA EM12"
        return types.SimpleNamespace(stdout=f"000000 +05.0 +0.50 1500 ~  {msg}\n")

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sparkgap, "_get_mqtt_publisher", lambda: None)
    sk = types.SimpleNamespace(cfg={"enable_ft8": True, "enable_ft4": True, "enable_rtty": True},
                               spot_count=0, managers=[], _corrected_freq_khz=lambda f: f,
                               broadcast=lambda **s: spots.append(s))
    z = np.zeros(60 * 192000, dtype=np.float32)
    sparkgap._ft8_cycle([("6m digital", 0, float(SIX_M_DIGITAL_KHZ), ft8_dial_khz(SIX_M_DIGITAL_KHZ),
                          z, z, len(z), 1792551360.0)], sk)
    by_mode = {s["mode"]: s for s in spots}
    assert by_mode["FT8"]["freq_khz"] == 50314.5 and by_mode["FT8"]["dx_call"] == "W5LDA"
    assert by_mode["FT4"]["freq_khz"] == 50319.5 and by_mode["FT4"]["dx_call"] == "K9EAG"
