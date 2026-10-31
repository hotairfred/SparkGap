"""itila2_parallel_decode: itila_sc_decode_ready decoding bins on several threads gives exactly the
serial loop's results (same bins, texts, speeds, costs and SNRs); only their order may differ."""

import ctypes
import math
import os
from pathlib import Path

import numpy as np
import pytest

MORSE = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", (
    ".- -... -.-. -.. . ..-. --. .... .. .--- -.- .-.. -- -. --- .--. --.- .-. ... - ..- ...- .-- -..- -.-- --.. "
    "----- .---- ..--- ...-- ....- ..... -.... --... ---.. ----.").split()))


def _keying(text: str, wpm: float, fs: int, n: int) -> np.ndarray:
    """On/off key of `text` repeated, with 5 ms raised-cosine edges."""
    u, seq = 1.2 / wpm, []
    for ch in text:
        if ch == " ":
            seq.append((0, 4 * u))
            continue
        for s in MORSE[ch]:
            seq += [(1, u if s == "." else 3 * u), (0, u)]
        seq.append((0, 2 * u))
    key, t = np.zeros(n), 0.0
    while t < n / fs:
        for on, d in seq:
            if on:
                key[int(t * fs):min(int((t + d) * fs), n)] = 1.0
            t += d
    w = int(0.005 * fs)
    edge = np.hanning(2 * w + 1)
    return np.convolve(key, edge / edge.sum(), mode="same")

ROOT = Path(__file__).resolve().parent.parent
DEC, SC = str(ROOT / "libitila2.so"), str(ROOT / "libitila2_scanner.so")
pytestmark = pytest.mark.skipif(
    not (os.path.exists(DEC) and os.path.exists(SC)), reason="libitila2 libraries not built"
)
D = ctypes.POINTER(ctypes.c_double)
vp = ctypes.c_void_p
REC = 288
STATIONS = [(-9000.0, "CQ TEST DE W1AW W1AW ", 25), (-4000.0, "CQ CWT K5KU K5KU ", 30),
            (1000.0, "TEST VE7NZ VE7NZ ", 22), (6000.0, "CQ DK9PY DK9PY TEST ", 28),
            (11000.0, "CQ N8KH N8KH ", 35), (15000.0, "TEST KA1EFO KA1EFO ", 26)]


def _libs():
    sc, dec = ctypes.CDLL(SC), ctypes.CDLL(DEC)
    sc.itila_sc_create.restype = vp
    sc.itila_sc_create.argtypes = [ctypes.c_int, ctypes.c_double, ctypes.c_int, ctypes.c_double, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_double, ctypes.c_double, ctypes.c_double, vp,
                                   ctypes.c_int, vp]
    sc.itila_sc_feed_iq.argtypes = [vp, D, D, ctypes.c_int]
    sc.itila_sc_set_snr_marks.argtypes = [vp, ctypes.c_int]
    sc.itila_sc_set_decoder.argtypes = [vp, vp, vp, vp, vp, ctypes.c_double]
    sc.itila_sc_set_decoder_pow.argtypes = [vp, vp, vp]
    sc.itila_sc_set_cost_fn.argtypes = [vp, vp]
    sc.itila_sc_set_decode_threads.restype = ctypes.c_int
    sc.itila_sc_set_decode_threads.argtypes = [vp, ctypes.c_int]
    sc.itila_sc_decode_ready.restype = ctypes.c_int
    sc.itila_sc_decode_ready.argtypes = [vp, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    sc.itila_sc_free.argtypes = [vp]
    return sc, dec


def _num(x: float):
    """SNR 500 as a comparable value: NaN (no reading) as None."""
    return None if math.isnan(x) else round(float(x), 4)


def _run(threads: int, snr: bool, max_results: int = 64) -> list:
    sc, dec = _libs()
    fs, win, block = 48_000, 6000, 4800
    n = 130 * fs
    rng = np.random.default_rng(7)
    t_all = np.arange(n) / fs
    z = math.sqrt(1e-4 / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    for off, text, wpm in STATIONS:
        z += 0.01 * _keying(text, wpm, fs, n) * np.exp(2j * np.pi * off * t_all)
    h = sc.itila_sc_create(fs, 7030000.0, 50, 12.0, win, 4096, 50.0, 7006000.0, 7054000.0, None, 0, None)
    cast = lambda f: ctypes.cast(f, vp)  # noqa: E731
    out = []
    try:
        if snr:
            assert sc.itila_sc_set_snr_marks(h, 1)
        sc.itila_sc_set_decoder(h, cast(dec.itila_create), cast(dec.itila_feed), cast(dec.itila_free),
                                cast(dec.itila_get_wpm), 1.5)
        sc.itila_sc_set_cost_fn(h, cast(dec.itila_get_last_cost))
        if snr:
            sc.itila_sc_set_decoder_pow(h, cast(dec.itila_feed_pow), cast(dec.itila_get_mark_power))
        assert sc.itila_sc_set_decode_threads(h, threads) == threads
        buf = ctypes.create_string_buffer(REC * max_results)
        for k in range(n // block + 20):                  # the last 20 calls only drain what is left
            if k < n // block:
                i = np.ascontiguousarray(z.real[k * block:(k + 1) * block])
                q = np.ascontiguousarray(z.imag[k * block:(k + 1) * block])
                sc.itila_sc_feed_iq(h, i.ctypes.data_as(D), q.ctypes.data_as(D), block)
            got = []
            for r in range(sc.itila_sc_decode_ready(h, win, buf, max_results)):
                o = r * REC
                got.append((round(ctypes.c_double.from_buffer(buf, o).value, 3),
                            ctypes.c_int.from_buffer(buf, o + 16).value,
                            _num(ctypes.c_float.from_buffer(buf, o + 20).value),
                            buf.raw[o + 24:o + 280].split(b"\0")[0].decode(),
                            round(ctypes.c_double.from_buffer(buf, o + 280).value, 6)))
            out.append(sorted(got, key=repr))
    finally:
        sc.itila_sc_free(h)
    return out


@pytest.mark.parametrize("snr", [False, True])
def test_parallel_decode_matches_the_serial_loop(snr: bool) -> None:
    serial, parallel = _run(1, snr), _run(4, snr)
    decoded = [r for call in serial for r in call]
    assert len(decoded) >= 2 * len(STATIONS)              # the stations really decoded
    assert any("W1AW" in r[3] for r in decoded) and any("KA1EFO" in r[3] for r in decoded)
    assert parallel == serial                             # call by call, the same records


def test_a_small_result_buffer_leaves_the_rest_for_the_next_call() -> None:
    """With room for fewer results than ready windows, nothing is lost: every window is decoded on a
    later call, as the serial loop does."""
    flat = lambda runs: sorted((r for call in runs for r in call), key=repr)  # noqa: E731
    assert flat(_run(4, False, max_results=4)) == flat(_run(1, False))
