"""band_slice_12k: the 12 kHz baseband cut from one FFT matches mixing + resample_poly."""

import numpy as np
from scipy.signal import resample_poly

import sparkgap

FS, N = 192000, 192000 * 4          # 4 s is enough for the comparison


def _old(fi, fq, off):
    x = fi.astype(np.float64) + 1j * fq.astype(np.float64)
    t = np.arange(len(x)) / FS
    return resample_poly(x * np.exp(-2j * np.pi * off * t), 1, 16)


def _tones(freqs, amps):
    t = np.arange(N) / FS
    x = sum(a * np.exp(2j * np.pi * f * t) for f, a in zip(freqs, amps))
    return x.real.astype(np.float32), x.imag.astype(np.float32)


def test_a_tone_lands_at_its_audio_frequency_with_the_same_level() -> None:
    off = -16000.0                                    # 40m: FT8 dial 16 kHz below the centre
    fi, fq = _tones([off + 1500.0], [1000.0])
    new = sparkgap.band_slice_12k(fi, fq, off)
    spec = np.abs(np.fft.fft(new))
    f = np.fft.fftfreq(len(new), 1 / 12000)
    assert abs(f[np.argmax(spec)] - 1500.0) < 1.0
    mid = slice(len(new) // 4, 3 * len(new) // 4)     # away from resample_poly's edge transients
    assert abs(np.abs(new[mid]).mean() / np.abs(_old(fi, fq, off)[mid]).mean() - 1) < 0.01


def test_it_matches_the_old_path_in_the_ft8_passband() -> None:
    rng = np.random.default_rng(1)
    off = 14074000.0 - 14090000.0
    freqs = off + rng.uniform(200, 3000, 12)          # FT8-like tones above the dial
    fi, fq = _tones(freqs, rng.uniform(100, 2000, 12))
    noise = rng.standard_normal((2, N)).astype(np.float32) * 50
    fi, fq = fi + noise[0], fq + noise[1]
    new, old = sparkgap.band_slice_12k(fi, fq, off), _old(fi, fq, off)
    n = min(len(new), len(old))
    band = lambda x: np.fft.fft(x[:n])[int(200 * n / 12000):int(3000 * n / 12000)]
    a, b = band(new), band(old)
    corr = np.abs(np.vdot(a, b)) / (np.linalg.norm(a) * np.linalg.norm(b))
    assert corr > 0.999


def test_single_and_double_precision_agree() -> None:
    fi, fq = _tones([-15000.0, -14000.0], [3000.0, 20.0])
    s = sparkgap.band_slice_12k(fi, fq, -16000.0, single=True)
    d = sparkgap.band_slice_12k(fi, fq, -16000.0, single=False)
    assert np.abs(s - d).max() < 1e-3 * np.abs(d).max()
