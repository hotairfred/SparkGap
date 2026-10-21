"""The RTTY signal search: one batched single-precision FFT gives the same max-hold spectrum as
a double-precision FFT per 2 s block, and finds a mark/space pair where it is."""
import numpy as np

import sparkgap

FS = 192000


def _old_hold(fi, fq, lo, hi):
    iq = fi.astype(np.float64) + 1j * fq.astype(np.float64)
    n = 2 * FS
    fr = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / FS))
    sel = (fr >= lo) & (fr <= hi)
    hold = None
    for k in range(0, len(iq) - n + 1, n):
        s = np.convolve(np.fft.fftshift(np.abs(np.fft.fft(iq[k:k + n])))[sel], np.ones(11) / 11, mode='same')
        hold = s if hold is None else np.maximum(hold, s)
    return fr[sel], hold


def test_the_hold_spectrum_matches_the_double_precision_one() -> None:
    rng = np.random.default_rng(5)
    fi, fq = (rng.standard_normal((2, 3 * 2 * FS + 999)) * 300).astype(np.float32)   # 3 blocks + a bit
    for lo, hi in ((-10000.0, 10000.0), (-55000.0, -35000.0), (30000.0, 60000.0)):   # across 0, below, above
        f_old, h_old = _old_hold(fi, fq, lo, hi)
        f_new, h_new = sparkgap._rtty_hold_spectrum(fi, fq, lo, hi)
        assert np.allclose(f_new, f_old)
        assert np.allclose(h_new, h_old, rtol=1e-3, atol=1e-3 * h_old.mean())


def test_a_mark_space_pair_is_found_at_its_centre() -> None:
    rng = np.random.default_rng(6)
    t = np.arange(6 * FS) / FS
    c = -41000.0                                       # 40m: 7049 kHz on a 7090 kHz receiver
    x = 30 * np.exp(2j * np.pi * (c - 85) * t) + 30 * np.exp(2j * np.pi * (c + 85) * t)
    x = x + (rng.standard_normal(len(t)) + 1j * rng.standard_normal(len(t))) * 3
    cands = sparkgap._rtty_candidates(x.real.astype(np.float32), x.imag.astype(np.float32),
                                      -55000.0, -35000.0)
    assert cands and abs(cands[0][0] - c) < 5.0       # smoothed peaks: within a few Hz
