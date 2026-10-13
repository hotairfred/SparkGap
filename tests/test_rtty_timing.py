"""librtty times each character from its start edge and drops characters with a space stop bit."""
import ctypes as ct
import os

import numpy as np
import pytest

from test_rtty_figures import CENTER, LIB, RATE, _afsk

pytestmark = pytest.mark.skipif(not os.path.exists(LIB), reason="librtty.so not built")


def _decode(audio: np.ndarray) -> str:
    lib = ct.CDLL(LIB)
    lib.rtty_create.restype = ct.c_void_p
    lib.rtty_create.argtypes = [ct.c_int, ct.c_double]
    lib.rtty_feed.restype = ct.c_char_p
    lib.rtty_feed.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double), ct.c_int, ct.POINTER(ct.c_double)]
    lib.rtty_free.argtypes = [ct.c_void_p]
    audio = np.ascontiguousarray(audio / np.max(np.abs(audio)) * 0.7)
    h = lib.rtty_create(RATE, CENTER)
    conf = ct.c_double(0.0)
    out = lib.rtty_feed(h, audio.ctypes.data_as(ct.POINTER(ct.c_double)), len(audio), ct.byref(conf))
    lib.rtty_free(h)
    return (out or b"").decode("ascii", "replace")


def test_weak_signal_copies_at_minus_3_db() -> None:
    # SNR -3 dB in 3 kHz, four noise draws, 8 calls each: timing from each start edge copies
    # 19 of the 32 calls, the free-running clock 6
    sig = _afsk("CQ TEST KN0L KN0L CQ " * 4)
    noise_w = np.sqrt(0.5 / 10 ** (-3 / 10) * (RATE / 2) / 3000)
    got = [_decode(sig + np.random.default_rng(s).standard_normal(len(sig)) * noise_w).count("KN0L")
           for s in range(4)]
    assert sum(got) >= 14, got


def test_noise_prints_few_characters() -> None:
    # pure noise: a space stop bit drops the character (about half the noise characters go)
    n = [len(_decode(np.random.default_rng(s).standard_normal(RATE * 20)).replace(" ", "")) for s in range(3)]
    assert sum(n) / len(n) < 60, n
