"""librtty copies through selective fading: one tone well below the other (ATC)."""
import ctypes as ct
import os

import numpy as np
import pytest

from test_rtty_figures import BAUD, CENTER, LIB, RATE, SHIFT, _codes


def _afsk(text: str, mark_db: float, space_db: float) -> np.ndarray:
    bits = [1] * 30
    for c in _codes(text):
        bits += [0] + [(c >> k) & 1 for k in range(5)] + [1, 1]
    bits += [1] * 30
    n = int(len(bits) * RATE / BAUD)
    level = np.array(bits)[np.minimum((np.arange(n) * BAUD / RATE).astype(int), len(bits) - 1)]
    freq = np.where(level == 1, CENTER + SHIFT / 2, CENTER - SHIFT / 2)
    amp = np.where(level == 1, 10 ** (mark_db / 20), 10 ** (space_db / 20))
    return amp * np.sin(2 * np.pi * np.cumsum(freq) / RATE)


@pytest.mark.skipif(not os.path.exists(LIB), reason="librtty.so not built")
@pytest.mark.parametrize("mark_db,space_db", [(-10.0, 0.0), (0.0, -10.0), (-20.0, 0.0)])
def test_selective_fading_still_copies(mark_db: float, space_db: float) -> None:
    lib = ct.CDLL(LIB)
    lib.rtty_create.restype = ct.c_void_p
    lib.rtty_create.argtypes = [ct.c_int, ct.c_double]
    lib.rtty_feed.restype = ct.c_char_p
    lib.rtty_feed.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double), ct.c_int, ct.POINTER(ct.c_double)]
    lib.rtty_free.argtypes = [ct.c_void_p]
    rng = np.random.default_rng(7)
    audio = _afsk("CQ TEST KN0L KN0L CQ " * 3, mark_db, space_db)
    audio = audio + 0.03 * rng.standard_normal(len(audio))
    audio = np.ascontiguousarray(audio / np.max(np.abs(audio)) * 0.7)
    h = lib.rtty_create(RATE, CENTER)
    conf = ct.c_double(0.0)
    out = lib.rtty_feed(h, audio.ctypes.data_as(ct.POINTER(ct.c_double)), len(audio), ct.byref(conf))
    lib.rtty_free(h)
    got = (out or b"").decode("ascii", "replace")
    assert got.count("KN0L") >= 5, got
