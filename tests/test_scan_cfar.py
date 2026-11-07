"""itila2 scanner energy scan: the exact early out before the local median keeps what it finds.
A steady tone well above the noise opens one bin at its frequency; noise alone opens none."""
import ctypes
from pathlib import Path

import numpy as np
import pytest

_LIB = Path(__file__).resolve().parent.parent / "libitila2_scanner.so"
if not _LIB.exists():
    pytest.skip("libitila2_scanner.so not built", allow_module_level=True)

FS, CENTER = 192000, 7030000.0
D = ctypes.POINTER(ctypes.c_double)


def _bins(tone_offsets_hz: list[float], amp: float, secs: float = 2.0) -> list[float]:
    sc = ctypes.CDLL(str(_LIB))
    vp = ctypes.c_void_p
    sc.itila_sc_create.restype = vp
    sc.itila_sc_create.argtypes = [ctypes.c_int, ctypes.c_double, ctypes.c_int, ctypes.c_double, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_double, ctypes.c_double, ctypes.c_double, vp,
                                   ctypes.c_int, vp]
    sc.itila_sc_feed_iq.argtypes = [vp, D, D, ctypes.c_int]
    sc.itila_sc_list_bins.restype = ctypes.c_int
    sc.itila_sc_list_bins.argtypes = [vp, D, ctypes.c_int]
    sc.itila_sc_free.argtypes = [vp]
    h = sc.itila_sc_create(FS, CENTER, 50, 12.0, FS * 30, 4096, 50.0, CENTER - 40000.0, CENTER + 40000.0,
                           None, 0, None)
    assert h
    try:
        rng = np.random.default_rng(3)
        n = int(FS * secs)
        t = np.arange(n) / FS
        z = 0.01 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
        for off in tone_offsets_hz:
            z = z + amp * np.exp(2j * np.pi * off * t)
        block = 4096
        for b in range(0, n - block, block):
            i = np.ascontiguousarray(z.real[b:b + block])
            q = np.ascontiguousarray(z.imag[b:b + block])
            sc.itila_sc_feed_iq(h, i.ctypes.data_as(D), q.ctypes.data_as(D), block)
        out = (ctypes.c_double * 64)()
        k = sc.itila_sc_list_bins(h, out, 64)
        return sorted(out[j] for j in range(k))
    finally:
        sc.itila_sc_free(h)


def test_strong_tones_open_bins_at_their_frequencies() -> None:
    got = _bins([-12000.0, 5000.0, 21000.0], amp=0.05)
    for off in (-12000.0, 5000.0, 21000.0):
        assert any(abs(f - (CENTER + off)) <= 50.0 for f in got), (off, got)


def test_noise_alone_opens_no_bin() -> None:
    assert _bins([], amp=0.0) == []
