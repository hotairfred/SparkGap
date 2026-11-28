"""itila_sc_set_pause: no new bins while paused (the operator transmits); open bins stay,
Play opens new ones."""

import ctypes as ct

import numpy as np
from test_scanner_fft_front import _keyed_iq, _scanner

P = ct.POINTER(ct.c_double)
FEED = 960  # ~5 ms feeds, as the live C path does


def _feed(lib: ct.CDLL, h: int, i: np.ndarray, q: np.ndarray) -> None:
    for p in range(0, len(i), FEED):
        a, b = i[p:p + FEED], q[p:p + FEED]
        lib.itila_sc_feed_iq(h, a.ctypes.data_as(P), b.ctypes.data_as(P), len(a))


def _bins(lib: ct.CDLL, h: int) -> list[float]:
    fb = np.empty(64)
    return fb[:lib.itila_sc_list_bins(h, fb.ctypes.data_as(P), 64)].tolist()


def _pausable() -> tuple[ct.CDLL, int]:
    lib, h = _scanner(False)
    lib.itila_sc_set_pause.restype = ct.c_int
    lib.itila_sc_set_pause.argtypes = [ct.c_void_p, ct.c_int]
    return lib, h


def test_paused_opens_no_bin_and_play_opens_it() -> None:
    lib, h = _pausable()
    i, q = _keyed_iq(seconds=3.0)
    assert lib.itila_sc_set_pause(h, 1) == 1
    _feed(lib, h, i, q)
    assert _bins(lib, h) == []
    assert lib.itila_sc_set_pause(h, 0) == 0
    _feed(lib, h, i, q)
    assert _bins(lib, h)
    lib.itila_sc_free(h)


def test_a_bin_open_before_the_pause_stays_open() -> None:
    lib, h = _pausable()
    i, q = _keyed_iq(seconds=3.0)
    _feed(lib, h, i, q)
    before = _bins(lib, h)
    assert before
    lib.itila_sc_set_pause(h, 1)
    _feed(lib, h, i, q)
    assert _bins(lib, h) == before
    lib.itila_sc_free(h)
