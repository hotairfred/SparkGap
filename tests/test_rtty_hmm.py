"""rtty_decoder "pick": the frame-model RTTY decoder (rtty_hmm.c) and the per-candidate pick."""
import ctypes as ct
import os

import numpy as np
import pytest

import sparkgap
from test_rtty_figures import BAUD, CENTER, LIB, RATE, SHIFT, _codes


def _afsk(text: str, snr_db: float, stop_bits: float = 1.5, seed: int = 0) -> np.ndarray:
    """AFSK at 12 kHz, mark high, characters back to back with the given stop, in noise (SNR in 250 Hz)."""
    bits: list[float] = []
    def put(level: int, n: float) -> None:
        bits.extend([level] * int(round(n * 2)))              # half-bit resolution for 1.5 stop bits
    put(1, 30)
    for c in _codes(text):
        put(0, 1)
        for k in range(5):
            put((c >> k) & 1, 1)
        put(1, stop_bits)
    put(1, 30)
    n = int(len(bits) / 2 * RATE / BAUD)
    level = np.array(bits)[np.minimum((np.arange(n) * 2 * BAUD / RATE).astype(int), len(bits) - 1)]
    f = np.where(level == 1, CENTER + SHIFT / 2, CENTER - SHIFT / 2)
    a = np.sin(2 * np.pi * np.cumsum(f) / RATE)
    noise = np.random.default_rng(seed).standard_normal(n) * np.sqrt(0.5 / 10 ** (snr_db / 10) * (RATE / 2) / 250)
    return np.ascontiguousarray(a + noise)


def _hmm(audio: np.ndarray) -> str:
    lib = sparkgap._get_rtty_lib()
    if lib is None or not hasattr(lib, "rtty_hmm_decode"):
        pytest.skip("librtty.so without rtty_hmm_decode")
    buf = ct.create_string_buffer(8192)
    lib.rtty_hmm_decode(audio.ctypes.data_as(ct.POINTER(ct.c_double)), len(audio), RATE, CENTER, buf, len(buf))
    return buf.value.decode("latin-1")


def _librtty(audio: np.ndarray) -> str:
    lib = sparkgap._get_rtty_lib()
    h = lib.rtty_create(RATE, CENTER); conf = ct.c_double(0.0)
    p = lib.rtty_feed(h, audio.ctypes.data_as(ct.POINTER(ct.c_double)), len(audio), ct.byref(conf))
    lib.rtty_free(h)
    return p.decode("latin-1").strip() if p else ""


MSG = "CQ TEST JA1BJI JA1BJI CQ TEST JA1BJI JA1BJI"
pytestmark = pytest.mark.skipif(not os.path.exists(LIB), reason="librtty.so not built")


@pytest.mark.parametrize("stop", [1.0, 1.5, 2.0])
def test_clean_signal_any_stop_length(stop: float) -> None:
    assert _hmm(_afsk(MSG, 25.0, stop)) == MSG


def test_weak_signal_copies_better_than_librtty() -> None:
    """At 3 dB (250 Hz) the frame model copies the call more often than librtty, summed over seeds."""
    hmm = lib = 0
    for seed in range(4):
        a = _afsk(MSG, 3.0, 1.5, seed)
        hmm += _hmm(a).count("JA1BJI"); lib += _librtty(a).count("JA1BJI")
    assert hmm > lib and hmm >= 8


def test_noise_and_silence_do_not_crash() -> None:
    rng = np.random.default_rng(1)
    _hmm(np.ascontiguousarray(rng.standard_normal(RATE * 20) * 0.3))
    assert _hmm(np.zeros(RATE * 5)) == ""
    assert _hmm(np.zeros(100)) == ""


def test_pick_keeps_the_text_naming_more_calls() -> None:
    assert sparkgap._rtty_pick("CQ TEST JA1B?I", "CQ TEST JA1BJI JA1BJI") == "CQ TEST JA1BJI JA1BJI"
    assert sparkgap._rtty_pick("CQ JA1BJI", "XQ JA1BJI") == "CQ JA1BJI"          # tie: librtty
    assert sparkgap._rtty_pick("", "") == ""


def test_config_key() -> None:
    assert sparkgap._rtty_decoder({}) == "librtty"
    assert sparkgap._rtty_decoder({"rtty_decoder": "pick"}) == "pick"
    assert sparkgap._rtty_decoder({"rtty_decoder": "PICK "}) == "pick"
    assert sparkgap._rtty_decoder({"rtty_decoder": "neural"}) == "librtty"
