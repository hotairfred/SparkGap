"""itila2_tone_lock_hz: a bin locks on the tone that keys most of its time and blanks a caller's short
over sent off it; a QSO of two equal stations, a zero-beat caller and a lone runner are left alone."""

import ctypes as ct

import numpy as np
import pytest
from test_scanner_fft_front import CENTER, FS, _scanner

P = ct.POINTER(ct.c_double)
F1 = 14028063.0                     # the runner's tone (off the FFT grid)
ENV_HZ = 200
DIT = 1.2 / 22
CODE = np.array([1, 1, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 1, 1, 0, 1, 0, 1, 1, 1, 0, 0, 0, 0, 0, 0])


def _scene(parts, seconds: float) -> tuple[np.ndarray, np.ndarray]:
    """parts: (start s, end s, hertz off the runner) of keyed CW at one level, in noise."""
    rng = np.random.default_rng(3)
    n = int(seconds * FS)
    t = np.arange(n) / FS
    z = 30.0 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    for t0, t1, df in parts:
        a, b = int(t0 * FS), int(t1 * FS)
        key = CODE[((t[a:b] - t0) / DIT).astype(int) % len(CODE)]
        z[a:b] += 3000.0 * key * np.exp(2j * np.pi * (F1 - CENTER + df) * t[a:b])
    return np.ascontiguousarray(z.real), np.ascontiguousarray(z.imag)


def _env(gate_hz, i, q):
    """The 200 Hz path's envelope of the bin nearest the runner, and (words, blanked)."""
    lib, h = _scanner(False)
    lib.itila_sc_set_tone_lock.restype = ct.c_double
    lib.itila_sc_set_tone_lock.argtypes = [ct.c_void_p, ct.c_double]
    lib.itila_sc_tone_lock_stats.argtypes = [ct.c_void_p, ct.POINTER(ct.c_ulonglong), ct.POINTER(ct.c_ulonglong)]
    if gate_hz is not None:
        assert lib.itila_sc_set_tone_lock(h, gate_hz) == gate_hz
    for p in range(0, len(i), 960):
        lib.itila_sc_feed_iq(h, i[p:p + 960].ctypes.data_as(P), q[p:p + 960].ctypes.data_as(P), len(i[p:p + 960]))
    fb = np.empty(64)
    bins = fb[:lib.itila_sc_list_bins(h, fb.ctypes.data_as(P), 64)].tolist()
    assert bins
    f = min(bins, key=lambda x: abs(x - F1))
    e1, e2 = np.empty(15000), np.empty(15000)
    n = lib.itila_sc_drain_env(h, f, e1.ctypes.data_as(P), e2.ctypes.data_as(P), 15000)
    w, bl = ct.c_ulonglong(), ct.c_ulonglong()
    lib.itila_sc_tone_lock_stats(h, ct.byref(w), ct.byref(bl))
    lib.itila_sc_free(h)
    return e2[:n].copy(), w.value, bl.value


def _peak(env, t0, t1, end_s):
    """The envelope's peak from t0 to t1 s; the envelope ends at end_s (the bin starts after the scene)."""
    off = len(env) - int(end_s * ENV_HZ)
    return env[max(0, int(t0 * ENV_HZ) + off):int(t1 * ENV_HZ) + off].max()


# A runner's CQs (7 s) and a caller's answer (2.5 s) at +25 Hz, five times over.
RUN = [(14 * k, 14 * k + 7, 0.0) for k in range(5)]
CALLER = [(14 * k + 8, 14 * k + 10.5, 25.0) for k in range(5)]


def test_off_is_untouched() -> None:
    """Never set, and set to 0, give the same envelope bit for bit."""
    i, q = _scene(RUN + CALLER, 70.0)
    a, _, _ = _env(None, i, q)
    b, w, bl = _env(0.0, i, q)
    assert np.array_equal(a, b) and w == 0 and bl == 0


def test_off_tone_caller_is_blanked_runner_kept() -> None:
    i, q = _scene(RUN + CALLER, 70.0)
    off, _, _ = _env(0.0, i, q)
    on, w, bl = _env(8.0, i, q)
    assert w > 0 and bl >= 3
    for k in range(1, 5):          # once locked: the caller's overs held at the noise, the runner untouched
        keyed = _peak(off, 14 * k + 8, 14 * k + 10.5, 70.0)
        assert _peak(on, 14 * k + 8.1, 14 * k + 10.4, 70.0) < 0.01 * keyed
        assert _peak(on, 14 * k, 14 * k + 7, 70.0) > 0.9 * keyed


def test_lone_runner_is_untouched() -> None:
    i, q = _scene(RUN, 70.0)
    off, _, _ = _env(0.0, i, q)
    on, w, bl = _env(8.0, i, q)
    assert w > 0 and bl == 0 and np.array_equal(off, on)


def test_zero_beat_caller_is_kept() -> None:
    """A caller within the gate (3 Hz off) is not told from the runner."""
    i, q = _scene(RUN + [(a, b, 3.0) for a, b, _ in CALLER], 70.0)
    off, _, _ = _env(0.0, i, q)
    on, _, bl = _env(8.0, i, q)
    assert bl == 0 and np.array_equal(off, on)


def test_qso_of_equal_stations_is_kept() -> None:
    """Two stations taking 5 s turns 25 Hz apart: once both have keyed, no tone dominates and nothing is
    blanked (the second station's first turn may be, as a caller's: the bin has heard only the first)."""
    parts = [(6 * k, 6 * k + 5, 25.0 * (k % 2)) for k in range(11)]
    i, q = _scene(parts, 66.0)
    off, _, _ = _env(0.0, i, q)
    on, _, _ = _env(8.0, i, q)
    tail = 54 * ENV_HZ                                 # from 12 s on
    assert len(on) == len(off) and np.array_equal(off[-tail:], on[-tail:])


def test_long_overs_are_kept() -> None:
    """An off-tone over with more than 6 s of key-down (about 12 s sent) is the other side of a
    conversation, kept."""
    parts = [(0, 20, 0.0), (21, 35, 25.0), (37, 52, 0.0), (53, 67, 25.0)]
    i, q = _scene(parts, 70.0)
    off, _, _ = _env(0.0, i, q)
    on, _, bl = _env(8.0, i, q)
    assert bl == 0 and np.array_equal(off, on)


@pytest.mark.parametrize("gate", [-1.0])
def test_negative_gate_is_off(gate: float) -> None:
    lib, h = _scanner(False)
    lib.itila_sc_set_tone_lock.restype = ct.c_double
    lib.itila_sc_set_tone_lock.argtypes = [ct.c_void_p, ct.c_double]
    assert lib.itila_sc_set_tone_lock(h, gate) == 0.0
    lib.itila_sc_free(h)
