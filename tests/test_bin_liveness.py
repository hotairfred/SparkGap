"""itila2_bin_liveness: a decoded window with an SCP call next to CQ/TEST marks its scanner bin live, so a
steady runner's bin is not evicted every 300 s; noise and bare calls are not evidence."""

import ctypes
import math
import os
from pathlib import Path

import numpy as np
import pytest

import sparkgap

ROOT = Path(__file__).resolve().parent.parent
DEC, SC = str(ROOT / "libitila2.so"), str(ROOT / "libitila2_scanner.so")
D = ctypes.POINTER(ctypes.c_double)
vp = ctypes.c_void_p
REC = 288
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


def _libs():
    sc, dec = ctypes.CDLL(SC), ctypes.CDLL(DEC)
    sc.itila_sc_create.restype = vp
    sc.itila_sc_create.argtypes = [ctypes.c_int, ctypes.c_double, ctypes.c_int, ctypes.c_double, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_double, ctypes.c_double, ctypes.c_double, vp,
                                   ctypes.c_int, vp]
    sc.itila_sc_feed_iq.argtypes = [vp, D, D, ctypes.c_int]
    sc.itila_sc_set_decoder.argtypes = [vp, vp, vp, vp, vp, ctypes.c_double]
    sc.itila_sc_decode_ready.restype = ctypes.c_int
    sc.itila_sc_decode_ready.argtypes = [vp, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    sc.itila_sc_free.argtypes = [vp]
    return sc, dec

KNOWN = {"W1AW", "K5KU", "VE7NZ"}


@pytest.mark.parametrize("raw, live", [
    ("CQ CWT CWT DE W1AW W1AW CWT", True),
    ("TEST K5KU TEST K5KU", True),
    ("TR EE EE ER TF EN T EE I", False),          # noise: the 4df58f5 case
    ("CQ CWT DE W9ZZZQ W9ZZZQ", False),            # call-shaped but not in MASTER.SCP
    ("W1AW 599 OH TU", False),                     # a known call, but no CQ/TEST: a caller or a QSO
])
def test_only_a_known_cq_call_is_evidence(raw: str, live: bool) -> None:
    assert sparkgap._copies_known_runner(raw, KNOWN) is live


def test_no_call_set_is_never_evidence() -> None:
    assert not sparkgap._copies_known_runner("CQ TEST W1AW W1AW", set())


def _bin_life(liveness: bool, secs: int = 400) -> list:
    """Feed one steady CQ for `secs`, decode through decode_ready, mark evidence as the spot-rule paths
    do with itila2_bin_liveness; return the times (s) the bin disappeared."""
    sc, dec = _libs()
    sc.itila_sc_list_bins.restype = ctypes.c_int
    sc.itila_sc_list_bins.argtypes = [vp, D, ctypes.c_int]
    sc.itila_sc_mark_evidence.argtypes = [vp, ctypes.c_double]
    fs, win, block = 48_000, 6000, 4800
    rng = np.random.default_rng(1)
    h = sc.itila_sc_create(fs, 7030000.0, 50, 12.0, win, 4096, 50.0, 7006000.0, 7054000.0, None, 0, None)
    cast = lambda f: ctypes.cast(f, vp)  # noqa: E731
    sc.itila_sc_set_decoder(h, cast(dec.itila_create), cast(dec.itila_feed), cast(dec.itila_free),
                            cast(dec.itila_get_wpm), 1.5)
    buf = ctypes.create_string_buffer(REC * 64)
    fl = (ctypes.c_double * 64)()
    gone, had = [], False
    chunk = 60 * fs
    try:
        for c0 in range(0, secs * fs, chunk):
            t = np.arange(c0, c0 + chunk) / fs
            key = _keying("CQ TEST DE W1AW W1AW ", 25, fs, chunk)
            z = math.sqrt(1e-4 / 2) * (rng.standard_normal(chunk) + 1j * rng.standard_normal(chunk))
            z = z + 0.01 * key * np.exp(2j * np.pi * 5000.0 * t)
            for b in range(0, chunk, block):
                i = np.ascontiguousarray(z.real[b:b + block])
                q = np.ascontiguousarray(z.imag[b:b + block])
                sc.itila_sc_feed_iq(h, i.ctypes.data_as(D), q.ctypes.data_as(D), block)
                for r in range(sc.itila_sc_decode_ready(h, win, buf, 64)):
                    f = ctypes.c_double.from_buffer(buf, r * REC).value
                    raw = buf.raw[r * REC + 24:r * REC + 280].split(b"\0")[0].decode()
                    if liveness and sparkgap._copies_known_runner(raw, KNOWN):
                        sc.itila_sc_mark_evidence(h, f)
                n = sc.itila_sc_list_bins(h, fl, 64)
                here = any(abs(fl[j] - 7035000.0) < 100 for j in range(n))
                if had and not here:
                    gone.append((c0 + b + block) / fs)
                had = here
    finally:
        sc.itila_sc_free(h)
    return gone


@pytest.mark.skipif(not (os.path.exists(DEC) and os.path.exists(SC)), reason="libitila2 libraries not built")
def test_a_steady_runner_keeps_its_bin_only_with_liveness() -> None:
    assert _bin_life(False)[:1] == [pytest.approx(300, abs=2)]   # today: evicted at 300 s
    assert _bin_life(True) == []                                  # with liveness: never
