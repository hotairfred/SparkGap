"""hpsdr_proxy WAV replay: the vectorized multi-receiver packet builder must
be byte-identical to the original per-sample _iq_packet for 1, 2 and 8
receivers. Runs under pytest, or standalone: python3 tests/test_hpsdr_proxy.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hpsdr_proxy as P  # noqa: E402


def _check(n_rx, seed):
    rng = np.random.default_rng(seed)
    k = 2 * (504 // (6 * n_rx + 2))
    blk = ((rng.random((k, n_rx, 2)) * 2 - 1) * 0.95).astype(np.float32)
    blk[0, 0] = (1.0, -1.0)                       # full-scale clipping path
    flat = [(float(blk[s, r, 0]), float(blk[s, r, 1])) for s in range(k) for r in range(n_rx)]
    assert P._iq_packet_np(3, blk) == P._iq_packet(3, flat, n_rx=n_rx)


def test_packet_identical_1rx():
    _check(1, 1)


def test_packet_identical_2rx():
    _check(2, 2)


def test_packet_identical_8rx():
    _check(8, 3)


def test_packet_layout():
    pkt = P._iq_packet_np(9, np.zeros((20, 8, 2), dtype=np.float32))
    assert len(pkt) == 1032 and pkt[:4] == b'\xef\xfe\x01\x06'
    assert pkt[8:11] == b'\x7f\x7f\x7f' and pkt[520:523] == b'\x7f\x7f\x7f'


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    ok = 0
    for fn in fns:
        try:
            fn(); print('PASS ', fn.__name__); ok += 1
        except Exception as e:
            print('FAIL ', fn.__name__, type(e).__name__, e)
    print(f'\n{ok}/{len(fns)} passed'); sys.exit(0 if ok == len(fns) else 1)
