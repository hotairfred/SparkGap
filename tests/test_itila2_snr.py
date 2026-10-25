"""itila2_snr_decoder: spot SNR in 500 Hz from the key-down power and the band's noise density."""

import ctypes
import math
import os
import re
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
DEC, SC = str(ROOT / "libitila2.so"), str(ROOT / "libitila2_scanner.so")
pytestmark = pytest.mark.skipif(
    not (os.path.exists(DEC) and os.path.exists(SC)), reason="libitila2 libraries not built"
)
D = ctypes.POINTER(ctypes.c_double)


def _ptr(a: np.ndarray):
    return a.ctypes.data_as(D)


def test_mark_power_is_the_mean_of_key_down_samples_two_in_from_each_edge() -> None:
    lib = ctypes.CDLL(DEC)
    lib.itila2_test_mark_power.restype = ctypes.c_double
    lib.itila2_test_mark_power.argtypes = [D, ctypes.POINTER(ctypes.c_int8), ctypes.c_int]
    marks = np.ascontiguousarray(np.tile([0] * 10 + [1] * 14, 10), dtype=np.int8)
    pw = np.where(marks == 1, 4.0, 1.0)
    pw[np.flatnonzero(np.diff(marks) == 1) + 1] = 100.0  # a transition sample: dropped
    pw = np.ascontiguousarray(pw)
    i8 = ctypes.POINTER(ctypes.c_int8)
    got = lib.itila2_test_mark_power(_ptr(pw), marks.ctypes.data_as(i8), len(pw))
    assert got == pytest.approx(4.0)
    few = np.ascontiguousarray(np.zeros(240, dtype=np.int8))
    few[:12] = 1
    short = np.ascontiguousarray(pw[:240])
    assert math.isnan(lib.itila2_test_mark_power(_ptr(short), few.ctypes.data_as(i8), 240))


def test_snr_formula_takes_the_path_noise_off_and_quotes_500_hz() -> None:
    lib = ctypes.CDLL(SC)
    lib.itila_sc_snr_from_power.restype = ctypes.c_double
    lib.itila_sc_snr_from_power.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_int]
    n0 = 1e-9
    assert lib.itila_sc_snr_from_power(n0 * (5000.0 + 197.1), n0, 1) == pytest.approx(10.0)
    assert lib.itila_sc_snr_from_power(n0 * (5000.0 + 36.5), n0, 0) == pytest.approx(10.0)
    assert math.isnan(lib.itila_sc_snr_from_power(n0 * 100.0, n0, 1))  # below the path noise
    assert math.isnan(lib.itila_sc_snr_from_power(1.0, 0.0, 1))  # no noise reading yet


def _taps(text: str, name: str) -> np.ndarray:
    body = re.search(name + r"\[[^\]]*\]\s*=\s*\{([^}]*)\}", text)
    assert body
    return np.array([float(x) for x in re.findall(r"[-+]?\d[\d.]*(?:[eE][-+]?\d+)?", body[1])])


def test_path_bandwidths_match_the_taps() -> None:
    coeffs = (ROOT / "itila_fir_coeffs.h").read_text()
    scanner = (ROOT / "itila2_scanner.c").read_text()
    f = np.linspace(-1000.0, 1000.0, 20001)

    def resp(t: np.ndarray, fs: float) -> np.ndarray:
        return np.abs(np.exp(-2j * np.pi * np.outer(f / fs, np.arange(len(t)))) @ t)

    hann = 0.5 - 0.5 * np.cos(2 * np.pi * (np.arange(80) + 0.5) / 80)
    wide = resp(_taps(coeffs, "FIR_S2_200"), 12000.0) * resp(_taps(scanner, "WIDE_TAPS"), 2000.0)
    narrow = resp(_taps(coeffs, "FIR_S2_100"), 12000.0) * resp(hann / hann.sum(), 2000.0)

    def constant(name: str) -> float:
        m = re.search(name + r"\s+([\d.]+)", scanner)
        assert m
        return float(m[1])

    assert np.trapezoid(wide**2, f) == pytest.approx(constant("SC_ENBW_200_HZ"), abs=0.5)
    assert np.trapezoid(narrow**2, f) == pytest.approx(constant("SC_ENBW_100_HZ"), abs=0.5)


_MORSE = {
    "C": "-.-.",
    "Q": "--.-",
    "T": "-",
    "E": ".",
    "S": "...",
    "D": "-..",
    "W": ".--",
    "1": ".----",
    "A": ".-",
}


def _keying(text: str, wpm: float, fs: int, n: int) -> np.ndarray:
    u, seq = 1.2 / wpm, []
    for ch in text:
        if ch == " ":
            seq.append((0, 4 * u))
            continue
        for s in _MORSE[ch]:
            seq += [(1, u if s == "." else 3 * u), (0, u)]
        seq.append((0, 2 * u))
    key, t = np.zeros(n), 0.0
    while t < n / fs:
        for on, d in seq:
            if on:
                key[int(t * fs) : min(int((t + d) * fs), n)] = 1.0
            t += d
    w = int(0.005 * fs)
    edge = np.hanning(2 * w + 1)
    return np.convolve(key, edge / edge.sum(), mode="same")


@pytest.mark.parametrize("true_db", [10.0, 30.0])
def test_keyed_cw_of_known_snr_reads_within_1_db(true_db: float) -> None:
    sc, dec = ctypes.CDLL(SC), ctypes.CDLL(DEC)
    sc.itila_sc_create.restype = ctypes.c_void_p
    sc.itila_sc_create.argtypes = [
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_double,
        ctypes.c_double,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
    ]
    sc.itila_sc_feed_iq.argtypes = [ctypes.c_void_p, D, D, ctypes.c_int]
    sc.itila_sc_set_snr_marks.argtypes = [ctypes.c_void_p, ctypes.c_int]
    sc.itila_sc_drain_env_pow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_double,
        D,
        D,
        D,
        D,
        ctypes.c_int,
        D,
    ]
    sc.itila_sc_snr_from_power.restype = ctypes.c_double
    sc.itila_sc_snr_from_power.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_int]
    sc.itila_sc_env_n.restype = ctypes.c_int
    sc.itila_sc_env_n.argtypes = [ctypes.c_void_p, ctypes.c_double]
    sc.itila_sc_list_bins.restype = ctypes.c_int
    sc.itila_sc_list_bins.argtypes = [ctypes.c_void_p, D, ctypes.c_int]
    sc.itila_sc_free.argtypes = [ctypes.c_void_p]
    dec.itila_create.restype = ctypes.c_void_p
    dec.itila_create.argtypes = [ctypes.c_int, ctypes.c_double]
    dec.itila_feed_pow.restype = ctypes.c_char_p
    dec.itila_feed_pow.argtypes = [
        ctypes.c_void_p,
        D,
        D,
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_double,
    ]
    dec.itila_get_mark_power.restype = ctypes.c_double
    dec.itila_get_mark_power.argtypes = [ctypes.c_void_p]
    dec.itila_free.argtypes = [ctypes.c_void_p]
    fs, win, block = 48_000, 6000, 4800
    rng = np.random.default_rng(1)
    noise_var = 1e-4
    amp = math.sqrt(10 ** (true_db / 10) * 500.0 * noise_var / fs)  # SNR in 500 Hz
    n = 65 * fs
    key = _keying("CQ TEST DE W1AW W1AW ", 25, fs, n)
    h = sc.itila_sc_create(
        fs, 7030000.0, 50, 12.0, win, 4096, 50.0, 7012000.0, 7048000.0, None, 0, None
    )
    hd = dec.itila_create(200, 200.0)
    got = []
    try:
        assert sc.itila_sc_set_snr_marks(h, 1)
        for k in range(n // block):
            t = np.arange(k * block, (k + 1) * block) / fs
            z = rng.standard_normal(block) + 1j * rng.standard_normal(block)
            z = math.sqrt(noise_var / 2) * z
            z = z + amp * key[k * block : (k + 1) * block] * np.exp(2j * np.pi * 5000.0 * t)
            i, q = np.ascontiguousarray(z.real), np.ascontiguousarray(z.imag)
            sc.itila_sc_feed_iq(h, _ptr(i), _ptr(q), block)
            bins = (ctypes.c_double * 64)()
            for j in range(sc.itila_sc_list_bins(h, bins, 64)):
                f = bins[j]
                if abs(f - 7035000.0) > 60 or sc.itila_sc_env_n(h, f) < win:
                    continue
                e1, e2, p1, p2 = (np.empty(win) for _ in range(4))
                n0 = ctypes.c_double()
                m = sc.itila_sc_drain_env_pow(
                    h, f, _ptr(e1), _ptr(e2), _ptr(p1), _ptr(p2), win, ctypes.byref(n0)
                )
                if dec.itila_feed_pow(hd, _ptr(e2), _ptr(p2), m, f / 1000.0, 1.5):
                    mark = dec.itila_get_mark_power(hd)
                    got.append(sc.itila_sc_snr_from_power(mark, n0.value, 1))
    finally:
        dec.itila_free(hd)
        sc.itila_sc_free(h)
    got = [g for g in got if math.isfinite(g)]
    assert got and abs(float(np.median(got)) - true_db) <= 1.0


def test_c_decode_loop_reports_snr500_in_the_result_record() -> None:
    """itila_sc_decode_ready (the HPSDR worker's path) fills ScDecodeResult.snr500, 288 bytes."""
    sc, dec = ctypes.CDLL(SC), ctypes.CDLL(DEC)
    vp = ctypes.c_void_p
    sc.itila_sc_create.restype = vp
    sc.itila_sc_create.argtypes = [
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_double,
        ctypes.c_double,
        vp,
        ctypes.c_int,
        vp,
    ]
    sc.itila_sc_feed_iq.argtypes = [vp, D, D, ctypes.c_int]
    sc.itila_sc_set_snr_marks.argtypes = [vp, ctypes.c_int]
    sc.itila_sc_set_decoder.argtypes = [vp, vp, vp, vp, vp, ctypes.c_double]
    sc.itila_sc_set_decoder_pow.argtypes = [vp, vp, vp]
    sc.itila_sc_decode_ready.restype = ctypes.c_int
    sc.itila_sc_decode_ready.argtypes = [vp, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    sc.itila_sc_free.argtypes = [vp]
    fs, win, block, true_db = 48_000, 6000, 4800, 20.0
    rng = np.random.default_rng(2)
    noise_var = 1e-4
    amp = math.sqrt(10 ** (true_db / 10) * 500.0 * noise_var / fs)
    n = 65 * fs
    key = _keying("CQ TEST DE W1AW W1AW ", 25, fs, n)
    h = sc.itila_sc_create(
        fs, 7030000.0, 50, 12.0, win, 4096, 50.0, 7012000.0, 7048000.0, None, 0, None
    )
    buf = ctypes.create_string_buffer(288 * 64)

    def cast(f):
        return ctypes.cast(f, vp)

    got = []
    try:
        assert sc.itila_sc_set_snr_marks(h, 1)
        sc.itila_sc_set_decoder(
            h,
            cast(dec.itila_create),
            cast(dec.itila_feed),
            cast(dec.itila_free),
            cast(dec.itila_get_wpm),
            1.5,
        )
        sc.itila_sc_set_decoder_pow(h, cast(dec.itila_feed_pow), cast(dec.itila_get_mark_power))
        for k in range(n // block):
            t = np.arange(k * block, (k + 1) * block) / fs
            z = rng.standard_normal(block) + 1j * rng.standard_normal(block)
            z = math.sqrt(noise_var / 2) * z
            z = z + amp * key[k * block : (k + 1) * block] * np.exp(2j * np.pi * 5000.0 * t)
            i, q = np.ascontiguousarray(z.real), np.ascontiguousarray(z.imag)
            sc.itila_sc_feed_iq(h, _ptr(i), _ptr(q), block)
            for r in range(sc.itila_sc_decode_ready(h, win, buf, 64)):
                f_hz = ctypes.c_double.from_buffer(buf, r * 288).value
                if abs(f_hz - 7035000.0) <= 60:
                    got.append(ctypes.c_float.from_buffer(buf, r * 288 + 20).value)
    finally:
        sc.itila_sc_free(h)
    got = [g for g in got if math.isfinite(g)]
    assert got and abs(float(np.median(got)) - true_db) <= 1.0
