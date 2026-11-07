"""itila2_fold_mask: stage 2 and the Hann / wide filters folded into the FFT mask, 2 kHz straight out,
give the same envelopes as the 12 kHz mask and the time-domain filters."""

import ctypes as ct

import numpy as np
import pytest
from test_scanner_fft_front import _keyed_iq, _scanner

P = ct.POINTER(ct.c_double)


def _fold_scanner():
    """An FFT front-end scanner whose library also declares itila_sc_set_fold."""
    lib, h = _scanner(False)
    lib.itila_sc_set_fold.restype = ct.c_int
    lib.itila_sc_set_fold.argtypes = [ct.c_void_p, ct.c_int]
    return lib, h


def _run(fold: bool, i: np.ndarray, q: np.ndarray) -> dict[float, np.ndarray]:
    lib, h = _fold_scanner()
    assert lib.itila_sc_set_fold(h, int(fold)) == int(fold)
    for p in range(0, len(i), 960):
        lib.itila_sc_feed_iq(h, i[p:p + 960].ctypes.data_as(P), q[p:p + 960].ctypes.data_as(P),
                             len(i[p:p + 960]))
    fb = np.empty(64)
    out = {}
    for f in fb[:lib.itila_sc_list_bins(h, fb.ctypes.data_as(P), 64)].tolist():
        e1, e2 = np.empty(15000), np.empty(15000)
        n = lib.itila_sc_drain_env(h, f, e1.ctypes.data_as(P), e2.ctypes.data_as(P), 15000)
        out[f] = np.stack([e1[:n], e2[:n]])
    lib.itila_sc_free(h)
    return out


def test_fold_matches_the_12_khz_chain() -> None:
    """Same bins; envelopes in step (no lag), correlated > 0.999 and within 1% in level on the keyed
    signal.  Not bit-close: no stage-2 decimation alias and double instead of float32 delay lines."""
    i, q = _keyed_iq()
    old, new = _run(False, i, q), _run(True, i, q)
    assert old and set(old) == set(new)
    keyed = 0
    for f in old:
        n = min(old[f].shape[1], new[f].shape[1])
        assert abs(old[f].shape[1] - new[f].shape[1]) <= 40     # 192 ms blocks: up to one behind
        for path in range(2):
            x, y = old[f][path][400:n], new[f][path][400:n]
            if np.percentile(x, 95) < 10 * np.percentile(x, 10) or np.percentile(x, 95) < 1000:
                continue
            keyed += 1
            c0 = np.corrcoef(x[1:-1], y[1:-1])[0, 1]
            assert c0 > 0.999
            assert c0 >= np.corrcoef(x[1:-1], y[:-2])[0, 1] and c0 >= np.corrcoef(x[1:-1], y[2:])[0, 1]
            assert abs(np.percentile(y, 95) / np.percentile(x, 95) - 1.0) < 0.01
    assert keyed >= 2


@pytest.mark.parametrize("offset_hz", [700.0, 1500.0, 3000.0])
def test_fold_strong_carrier_stays_out_of_the_gaps(offset_hz: float) -> None:
    """A carrier 60 dB above a weak keyed signal: the gaps stay at the noise floor (the causal
    response's start-up, dropped each block, would otherwise wrap the carrier into them)."""
    i, q = _keyed_iq(carrier_db=60.0, carrier_hz=offset_hz, noise=3.0)
    old, new = _run(False, i, q), _run(True, i, q)
    f = min(old, key=lambda k: abs(k - 14028063.0))
    assert abs(f - 14028063.0) < 50.0 and f in new
    env = new[f][0][400:]
    assert np.percentile(env, 95) > 100 * np.percentile(env, 20)
    assert np.percentile(env, 20) < 2.0 * np.percentile(old[f][0][400:], 20) + 1e-9


def test_fold_only_before_the_first_iq() -> None:
    """Once IQ is fed the block size is fixed: the setter keeps the state in force."""
    lib, h = _fold_scanner()
    i, q = _keyed_iq(seconds=0.1)
    lib.itila_sc_feed_iq(h, i.ctypes.data_as(P), q.ctypes.data_as(P), len(i))
    assert lib.itila_sc_set_fold(h, 1) == 0
    lib.itila_sc_free(h)
    lib, h = _fold_scanner()
    assert lib.itila_sc_set_fold(h, 1) == 1 and lib.itila_sc_set_fold(h, 0) == 0
    lib.itila_sc_free(h)
