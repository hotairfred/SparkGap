"""librtty's matched filters reject a louder RTTY neighbour 250 Hz away."""
import ctypes as ct
import os

import numpy as np
import pytest

from test_rtty_figures import BAUD, CENTER, LIB, RATE, SHIFT, _codes


def _afsk_at(text: str, center: float, amp: float, n: int) -> np.ndarray:
    bits = [1] * 30
    for c in _codes(text):
        bits += [0] + [(c >> k) & 1 for k in range(5)] + [1, 1]
    bits += [1] * 30
    level = np.array(bits)[np.minimum((np.arange(n) * BAUD / RATE).astype(int), len(bits) - 1)]
    freq = np.where(level == 1, center + SHIFT / 2, center - SHIFT / 2)
    return amp * np.sin(2 * np.pi * np.cumsum(freq) / RATE)


@pytest.mark.skipif(not os.path.exists(LIB), reason="librtty.so not built")
def test_louder_neighbour_250_hz_away() -> None:
    # the neighbour's space tone sits 80 Hz from our mark: matched filters copy 12 of 24, the 60 Hz
    # resonators 0
    lib = ct.CDLL(LIB)
    lib.rtty_create.restype = ct.c_void_p
    lib.rtty_create.argtypes = [ct.c_int, ct.c_double]
    lib.rtty_feed.restype = ct.c_char_p
    lib.rtty_feed.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double), ct.c_int, ct.POINTER(ct.c_double)]
    lib.rtty_free.argtypes = [ct.c_void_p]
    n = int(RATE * 12)
    copied = 0
    for seed in range(4):
        audio = (_afsk_at("CQ TEST KN0L KN0L CQ " * 3, CENTER, 1.0, n)
                 + _afsk_at("QRZ DE W1AW W1AW 599 TU " * 4, CENTER + 250, 10 ** (10 / 20), n)
                 + np.random.default_rng(seed).standard_normal(n) * 0.05)
        audio = np.ascontiguousarray(audio / np.max(np.abs(audio)) * 0.7)
        h = lib.rtty_create(RATE, CENTER)
        conf = ct.c_double(0.0)
        out = lib.rtty_feed(h, audio.ctypes.data_as(ct.POINTER(ct.c_double)), n, ct.byref(conf))
        lib.rtty_free(h)
        copied += (out or b"").decode("ascii", "replace").count("KN0L")
    assert copied >= 8, copied
