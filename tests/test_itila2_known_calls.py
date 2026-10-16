"""itila2 known-call prior (itila_set_known_calls): a call heard lately is kept whole when the gap
inside it is borderline (K1VUT, not K1V UT); a clear word gap is left alone; off by default."""
import ctypes as ct
from pathlib import Path

import numpy as np
import pytest

_LIB = Path(__file__).resolve().parent.parent / "libitila2.so"
if not _LIB.exists():
    pytest.skip("libitila2.so not built", allow_module_level=True)
lib = ct.CDLL(str(_LIB))
lib.itila_create.restype = ct.c_void_p
lib.itila_create.argtypes = [ct.c_int, ct.c_double]
lib.itila_feed.restype = ct.c_char_p
lib.itila_feed.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double), ct.c_int, ct.c_double, ct.c_double]
lib.itila_free.argtypes = [ct.c_void_p]
lib.itila_set_known_calls.restype = ct.c_int
lib.itila_set_known_calls.argtypes = [ct.c_char_p, ct.c_int, ct.c_double]

MORSE = {'C': '-.-.', 'Q': '--.-', 'D': '-..', 'E': '.', 'K': '-.-', 'V': '...-', 'U': '..-',
         'T': '-', '1': '.----'}


def _env(stretch_units: float, unit: int = 12) -> np.ndarray:
    """200 Hz envelope of CQ CQ DE K1VUT K1VUT K, the V-U gap stretched to stretch_units."""
    out: list[float] = []
    for wi, w in enumerate("CQ CQ DE K1VUT K1VUT K".split()):
        if wi:
            out += [0] * 7 * unit
        for ci, ch in enumerate(w):
            if ci:
                out += [0] * int((stretch_units if (w, ci) == ("K1VUT", 3) else 3) * unit)
            for ei, el in enumerate(MORSE[ch]):
                if ei:
                    out += [0] * unit
                out += [1] * (unit if el == '.' else 3 * unit)
    x = np.array([0] * 200 + out + [0] * 400, float)
    return np.ascontiguousarray(0.05 + x + 0.08 * np.random.default_rng(0).standard_normal(len(x)))


def _decode(env: np.ndarray) -> str:
    h = lib.itila_create(200, 200.0)
    try:
        r = lib.itila_feed(h, env.ctypes.data_as(ct.POINTER(ct.c_double)), len(env), 7030.0, 1.5)
        return r.decode() if r else ''
    finally:
        lib.itila_free(h)


@pytest.fixture(autouse=True)
def _clear():
    lib.itila_set_known_calls(None, 0, 0.0)
    yield
    lib.itila_set_known_calls(None, 0, 0.0)


def test_borderline_gap_keeps_a_known_call_whole() -> None:
    env = _env(5.0)
    assert "K1V UT" in _decode(env)                       # no list: the fragment bust
    assert lib.itila_set_known_calls(b"K1VUT\nW1AW\nK1VUT", 17, 2.0) == 2
    assert "K1VUT K1VUT" in _decode(env)


def test_clear_word_gap_is_not_forced() -> None:
    lib.itila_set_known_calls(b"K1VUT", 5, 2.0)
    assert "K1V UT" in _decode(_env(6.0))


def test_unknown_call_unchanged_and_clearing_works() -> None:
    env = _env(5.0)
    plain = _decode(env)
    lib.itila_set_known_calls(b"W1AW\nK1ABC", 10, 2.0)    # K1VUT not on the list
    assert _decode(env) == plain
    lib.itila_set_known_calls(b"K1VUT", 5, 0.0)           # bonus 0 clears
    assert _decode(env) == plain


def test_short_calls_earn_no_bonus() -> None:
    """A 1x1 call on the list (K1V here) must not pull K1VUT apart."""
    env = _env(4.0)                                     # a normal letter gap: K1VUT whole
    whole = _decode(env)
    lib.itila_set_known_calls(b"K1V", 3, 4.0)
    assert _decode(env) == whole
