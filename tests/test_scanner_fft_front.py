"""itila2 scanner stage 1: the shared FFT front end gives the same envelopes as the per-bin mix + FIR1."""

import ctypes as ct
import os
from pathlib import Path

import numpy as np
import pytest

LIB = Path(__file__).resolve().parent.parent / "libitila2_scanner.so"
FS = 192000
CENTER = 14090000.0


def _scanner(fir1: bool):
    """A scanner made with ITILA_SC_FIR1 set (old path) or unset (FFT front end)."""
    if not LIB.exists():
        pytest.skip("libitila2_scanner.so not built")
    lib = ct.CDLL(str(LIB))
    lib.itila_sc_create.restype = ct.c_void_p
    lib.itila_sc_create.argtypes = [ct.c_int, ct.c_double, ct.c_int, ct.c_double, ct.c_int, ct.c_int,
                                    ct.c_double, ct.c_double, ct.c_double,
                                    ct.c_void_p, ct.c_int, ct.c_void_p]
    lib.itila_sc_feed_iq.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double), ct.POINTER(ct.c_double), ct.c_int]
    lib.itila_sc_list_bins.restype = ct.c_int
    lib.itila_sc_list_bins.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double), ct.c_int]
    lib.itila_sc_drain_env.restype = ct.c_int
    lib.itila_sc_drain_env.argtypes = [ct.c_void_p, ct.c_double, ct.POINTER(ct.c_double),
                                       ct.POINTER(ct.c_double), ct.c_int]
    lib.itila_sc_free.argtypes = [ct.c_void_p]
    old = os.environ.pop("ITILA_SC_FIR1", None)
    try:
        if fir1:
            os.environ["ITILA_SC_FIR1"] = "1"
        h = lib.itila_sc_create(FS, CENTER, 64, 12.0, 12000, 4096, 50.0,
                                14000000.0, 14070000.0, None, 0, None)
    finally:
        os.environ.pop("ITILA_SC_FIR1", None)
        if old is not None:
            os.environ["ITILA_SC_FIR1"] = old
    assert h
    return lib, h


def _keyed_iq(seconds: float = 12.0) -> tuple[np.ndarray, np.ndarray]:
    """A keyed CW tone 61.937 kHz below centre (off the FFT grid) plus a steady carrier 12 kHz above it, in noise."""
    rng = np.random.default_rng(1)
    n = int(seconds * FS)
    t = np.arange(n) / FS
    dit = 1.2 / 22                                     # 22 WPM
    pattern = np.array([1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0, 0, 0])
    key = pattern[(t / dit).astype(int) % len(pattern)].astype(float)
    f1 = 14028063.0 - CENTER
    sig = 3000.0 * key * np.exp(2j * np.pi * f1 * t) + 3000.0 * np.exp(2j * np.pi * (f1 + 12000.0) * t)
    noise = 30.0 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    z = sig + noise
    return np.ascontiguousarray(z.real), np.ascontiguousarray(z.imag)


def _run(fir1: bool, i: np.ndarray, q: np.ndarray) -> dict[float, np.ndarray]:
    lib, h = _scanner(fir1)
    P = ct.POINTER(ct.c_double)
    for p in range(0, len(i), 960):                    # ~5 ms feeds, as the live C path does
        lib.itila_sc_feed_iq(h, i[p:p + 960].ctypes.data_as(P), q[p:p + 960].ctypes.data_as(P),
                             len(i[p:p + 960]))
    fb = np.empty(64)
    out = {}
    for f in fb[:lib.itila_sc_list_bins(h, fb.ctypes.data_as(P), 64)].tolist():
        e1, e2 = np.empty(15000), np.empty(15000)
        n = lib.itila_sc_drain_env(h, f, e1.ctypes.data_as(P), e2.ctypes.data_as(P), 15000)
        out[f] = np.stack([e1[:n], e2[:n]])
    lib.itila_sc_free(h)
    return out


def test_fft_front_matches_fir1_envelopes() -> None:
    """Same bins and envelope length; on the keyed signal both paths correlate > 0.9999 and agree within 1%."""
    i, q = _keyed_iq()
    old, new = _run(True, i, q), _run(False, i, q)
    assert old and set(old) == set(new)
    keyed = 0
    for f in old:
        a, b = old[f], new[f]
        assert a.shape == b.shape
        for path in range(2):
            x, y = a[path][400:], b[path][400:]         # skip the filters' start-up
            if np.percentile(x, 95) < 10 * np.percentile(x, 10) or np.percentile(x, 95) < 1000:
                continue                                # carrier or noise only: correlation means nothing
            keyed += 1
            assert np.corrcoef(x, y)[0, 1] > 0.9999
            assert abs(np.percentile(y, 95) / np.percentile(x, 95) - 1.0) < 0.01
    assert keyed >= 2


def test_fft_front_drops_the_12_khz_alias() -> None:
    """The carrier 12 kHz from the keyed signal folds onto it through FIR1 (40 dB); the FFT front end has no fold."""
    i, q = _keyed_iq()
    old, new = _run(True, i, q), _run(False, i, q)
    f = min(old, key=lambda k: abs(k - 14028063.0))
    up_old, up_new = np.percentile(old[f][0][400:], 10), np.percentile(new[f][0][400:], 10)
    assert up_new < up_old
