"""librtty decodes figures (digits) to the right characters: a call with a digit survives."""
import ctypes as ct
import os

import numpy as np
import pytest

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "librtty.so")
RATE, CENTER, SHIFT, BAUD = 12000, 1000.0, 170.0, 45.45
LTRS = "\0E\nA SIU\rDRJNFCKTZLWHYPQOBG\0MXV\0"
FIGS = "\0" + "3\n- \a87\r$4',!:(5\")2#6019?&" + "\0./;\0"
SHIFT_LTRS, SHIFT_FIGS = 0x1F, 0x1B


def _codes(text: str) -> list[int]:
    out, figs = [SHIFT_LTRS, SHIFT_LTRS], False
    for ch in text:
        if ch in LTRS and ch != "\0" and (not figs or ch == " "):
            out.append(LTRS.index(ch))
        elif ch in FIGS and ch != "\0" and (figs or ch == " "):
            out.append(FIGS.index(ch))
        elif ch in LTRS:
            out += [SHIFT_LTRS, LTRS.index(ch)]; figs = False
        else:
            out += [SHIFT_FIGS, FIGS.index(ch)]; figs = True
    return out


def _afsk(text: str) -> np.ndarray:
    """Clean AFSK RTTY at 45.45 Bd, mark high: idle mark, start 0, 5 data bits LSB first, 1.5 stop."""
    bits = [1] * 30
    for c in _codes(text):
        bits += [0] + [(c >> k) & 1 for k in range(5)] + [1, 1]   # 2 stop bits: a generous 1.5
    bits += [1] * 30
    spb = RATE / BAUD
    n = int(len(bits) * spb)
    level = np.array([bits[min(int(i / spb), len(bits) - 1)] for i in range(n)])
    freq = np.where(level == 1, CENTER + SHIFT / 2, CENTER - SHIFT / 2)
    return (0.7 * np.sin(2 * np.pi * np.cumsum(freq) / RATE)).astype(np.float64)


@pytest.mark.skipif(not os.path.exists(LIB), reason="librtty.so not built")
def test_figures_decode_to_the_right_digits() -> None:
    lib = ct.CDLL(LIB)
    lib.rtty_create.restype = ct.c_void_p
    lib.rtty_create.argtypes = [ct.c_int, ct.c_double]
    lib.rtty_feed.restype = ct.c_char_p
    lib.rtty_feed.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double), ct.c_int, ct.POINTER(ct.c_double)]
    lib.rtty_free.argtypes = [ct.c_void_p]
    text = "CQ TEST KN0L KN0L NR4O W1AW K2LE N5AW 0123456789 "
    audio = _afsk(text * 2)
    h = lib.rtty_create(RATE, CENTER)
    conf = ct.c_double(0.0)
    out = lib.rtty_feed(h, audio.ctypes.data_as(ct.POINTER(ct.c_double)), len(audio), ct.byref(conf))
    lib.rtty_free(h)
    got = (out or b"").decode("ascii", "replace")
    for word in ("KN0L", "NR4O", "W1AW", "K2LE", "N5AW", "0123456789"):
        assert word in got, (word, got)
