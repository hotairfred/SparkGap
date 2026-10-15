"""rtty_spot_rule "repeat": an RTTY CQ cut from wideband IQ spots after tier(call) minutes."""
import os
import types

import numpy as np
import pytest

import sparkgap
from spot_rule import RepeatSpotRule
from test_rtty_figures import BAUD, LIB, SHIFT, _codes

RATE = 192000


def _iq(text: str, offset_hz: float, seconds: float, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """RTTY at offset_hz from the band centre (mark high), in noise, as one IQ snapshot."""
    bits = [1] * 20
    for c in _codes(text):
        bits += [0] + [(c >> k) & 1 for k in range(5)] + [1, 1]
    n = int(seconds * RATE)
    k = np.minimum((np.arange(n) * BAUD / RATE).astype(int), len(bits) - 1)
    level = np.array(bits)[k]
    freq = offset_hz + np.where(level == 1, SHIFT / 2, -SHIFT / 2)
    sig = 0.1 * np.exp(2j * np.pi * np.cumsum(freq) / RATE)
    rng = np.random.default_rng(seed)
    sig = sig + 0.01 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return sig.real.astype(np.float32), sig.imag.astype(np.float32)


@pytest.mark.skipif(not os.path.exists(LIB), reason="librtty.so not built")
def test_rtty_cq_spots_after_two_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    spots = []
    sk = types.SimpleNamespace(
        broadcast=lambda **k: spots.append((k["freq_khz"], k["dx_call"])),
        spot_count=0, _corrected_freq_khz=lambda f: f, cfg={"rtty_spot_rule": "repeat"},
        _rtty_rule=RepeatSpotRule(lambda c: 2))
    lib = sparkgap._get_rtty_lib()
    text = "CQ TEST KN0L KN0L CQ " * 6
    for minute in range(2):
        fi, fq = _iq(text, 5000.0, 20.0, minute)
        monkeypatch.setattr(sparkgap, "time", types.SimpleNamespace(time=lambda m=minute: 1791669600.0 + 60 * (m + 1)))
        sparkgap._rtty_scan_band_repeat(sk, lib, "20m", 14090, fi, fq, len(fi))
        if minute == 0:
            assert spots == []                    # one minute is one copy
    assert [c for _, c in spots] == ["KN0L"]
    assert abs(spots[0][0] - 14095.0) < 0.1


@pytest.mark.skipif(not os.path.exists(LIB), reason="librtty.so not built")
def test_rtty_rule_is_cw_rule_without_qualifiers() -> None:
    # qualifiers let an RTTY caller through on a contest recording (TU KQ4TNF CQ N4TL N4TL .. 59 N4TL FM06)
    sk = types.SimpleNamespace(telnet=None, spot_count=0, _corrected_freq_khz=lambda f: f,
                               cfg={"rtty_spot_rule": "repeat", "spot_rule_cq_qualifiers": True},
                               tracker=sparkgap.SpotTracker(set(), set()))
    fi = np.zeros(2 * RATE, dtype=np.float32)
    sparkgap._rtty_scan_band_repeat(sk, sparkgap._get_rtty_lib(), "20m", 14090, fi, fi.copy(), len(fi))
    r = sk._rtty_rule
    assert (r.lockon, r.cq_groups, r.cq_qualifiers, r.split_runs) == (True, True, False, False)
