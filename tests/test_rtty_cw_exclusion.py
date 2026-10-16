"""rtty_excludes_cw: CW bins are kept off RTTY tones, and only real FSK counts as RTTY."""
import ctypes as ct
import os

import numpy as np
import pytest

from test_rtty_figures import BAUD, LIB, SHIFT, _codes

SC_LIB = "./libitila2_scanner.so"
RATE = 12000


def _rtty_audio(text: str, seconds: float, seed: int) -> np.ndarray:
    """RTTY audio centred on 1000 Hz (mark high), in light noise."""
    bits = [1] * 20
    for c in _codes(text):
        bits += [0] + [(c >> k) & 1 for k in range(5)] + [1, 1]
    n = int(seconds * RATE)
    k = np.minimum((np.arange(n) * BAUD / RATE).astype(int), len(bits) - 1)
    f = 1000.0 + np.where(np.array(bits)[k] == 1, SHIFT / 2, -SHIFT / 2)
    x = np.sin(2 * np.pi * np.cumsum(f) / RATE)
    return x + 0.05 * np.random.default_rng(seed).standard_normal(n)


def _cw_pair_audio(seconds: float, seed: int) -> np.ndarray:
    """Two CW stations 170 Hz apart (915 and 1085 Hz), keyed independently at ~25 WPM."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    out = 0.05 * rng.standard_normal(n)
    for f in (915.0, 1085.0):
        dit = int(0.048 * RATE)
        key = np.zeros(n)
        i = 0
        while i < n:
            on = dit * rng.choice([1, 3])
            key[i:i + on] = 1.0
            i += on + dit * rng.choice([1, 1, 3, 7])
        out += key * np.sin(2 * np.pi * f * t)
    return out


def _fsk_seconds(lib, audio: np.ndarray) -> int:
    h = lib.rtty_create(ct.c_int(RATE), ct.c_double(1000.0))
    conf = ct.c_double(0.0)
    a = np.ascontiguousarray(audio, dtype=np.float64)
    lib.rtty_feed(h, a.ctypes.data_as(ct.POINTER(ct.c_double)), ct.c_int(len(a)), ct.byref(conf))
    s = lib.rtty_fsk_seconds(h)
    lib.rtty_free(h)
    return s


@pytest.mark.skipif(not os.path.exists(LIB), reason="librtty.so not built")
def test_fsk_seconds_tell_rtty_from_two_cw_stations() -> None:
    lib = ct.CDLL(LIB)
    lib.rtty_create.restype = ct.c_void_p
    lib.rtty_create.argtypes = [ct.c_int, ct.c_double]
    lib.rtty_feed.restype = ct.c_char_p
    lib.rtty_feed.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double), ct.c_int, ct.POINTER(ct.c_double)]
    lib.rtty_free.argtypes = [ct.c_void_p]
    lib.rtty_fsk_seconds.restype = ct.c_int
    lib.rtty_fsk_seconds.argtypes = [ct.c_void_p]
    import sparkgap
    assert _fsk_seconds(lib, _rtty_audio("CQ TEST KN0L KN0L CQ " * 4, 20.0, 1)) >= sparkgap.RTTY_EXCL_MIN_FSK_S
    for seed in range(5):
        assert _fsk_seconds(lib, _cw_pair_audio(20.0, seed)) < sparkgap.RTTY_EXCL_MIN_FSK_S


def _scanner():
    lib = ct.CDLL(SC_LIB)
    lib.itila_sc_create.restype = ct.c_void_p
    lib.itila_sc_create.argtypes = [ct.c_int, ct.c_double, ct.c_int, ct.c_double, ct.c_int, ct.c_int,
                                    ct.c_double, ct.c_double, ct.c_double,
                                    ct.c_void_p, ct.c_int, ct.c_void_p]
    lib.itila_sc_feed_iq.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double), ct.POINTER(ct.c_double), ct.c_int]
    lib.itila_sc_bin_count.restype = ct.c_int
    lib.itila_sc_bin_count.argtypes = [ct.c_void_p]
    lib.itila_sc_exclude.argtypes = [ct.c_void_p, ct.c_double, ct.c_double, ct.c_double]
    for f in ("itila_sc_excl_released", "itila_sc_excl_blocked"):
        getattr(lib, f).restype = ct.c_ulonglong
        getattr(lib, f).argtypes = [ct.c_void_p]
    lib.itila_sc_free.argtypes = [ct.c_void_p]
    lib.itila_sc_list_bins.restype = ct.c_int
    lib.itila_sc_list_bins.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double), ct.c_int]
    h = lib.itila_sc_create(192000, 7030000.0, 50, 12.0, 4000, 4096, 10.0, 7000000.0, 7060000.0,
                            None, 0, None)
    return lib, h


def _feed(lib, h, seconds: float, offsets_hz) -> None:
    rate, block = 192000, 19200
    rng = np.random.default_rng(3)
    for k in range(int(seconds * rate / block)):
        t = (k * block + np.arange(block)) / rate
        z = 0.01 * (rng.standard_normal(block) + 1j * rng.standard_normal(block))
        for f in offsets_hz:
            z = z + 0.3 * np.exp(2j * np.pi * f * t)
        i = np.ascontiguousarray(z.real)
        q = np.ascontiguousarray(z.imag)
        lib.itila_sc_feed_iq(h, i.ctypes.data_as(ct.POINTER(ct.c_double)),
                             q.ctypes.data_as(ct.POINTER(ct.c_double)), block)


def _near(lib, h, f_hz: float) -> bool:
    buf = (ct.c_double * 64)()
    n = lib.itila_sc_list_bins(h, buf, 64)
    return any(abs(buf[i] - f_hz) < 100.0 for i in range(n))


@pytest.mark.skipif(not os.path.exists(SC_LIB), reason="libitila2_scanner.so not built")
def test_scanner_keeps_bins_off_an_excluded_span() -> None:
    lib, h = _scanner()
    try:
        _feed(lib, h, 3.0, [5000.0, 12000.0])           # carriers at 7035.0 and 7042.0 kHz
        assert _near(lib, h, 7035000.0) and _near(lib, h, 7042000.0)
        lib.itila_sc_exclude(h, 7034850.0, 7035150.0, 2.0)
        _feed(lib, h, 1.0, [5000.0, 12000.0])
        assert not _near(lib, h, 7035000.0) and _near(lib, h, 7042000.0)   # released, neighbour kept
        assert lib.itila_sc_excl_released(h) == 1 and lib.itila_sc_excl_blocked(h) > 0
        _feed(lib, h, 4.0, [5000.0, 12000.0])            # the zone lapses after 2 s
        assert _near(lib, h, 7035000.0)
    finally:
        lib.itila_sc_free(h)
