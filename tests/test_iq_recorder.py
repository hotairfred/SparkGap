"""All-band IQ recorder (iq_recorder.py): bit-exact 24-bit output from the
FT8 swap buffers' raw 24-bit values, append across swaps, gap logging,
RECORD-file start/stop, 16-bit path.

Runs under pytest, or standalone:  python3 tests/test_iq_recorder.py
"""
import glob
import logging
import os
import sys
import tempfile
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import iq_recorder as R  # noqa: E402

RATE = 1000   # small rate keeps the test fast; the recorder is rate-agnostic


def _swap(rng, n):
    """Raw 24-bit sample values as float32, like hpsdr_fast's FT8 buffers."""
    i = rng.integers(-2**23, 2**23, n).astype(np.float32)
    q = rng.integers(-2**23, 2**23, n).astype(np.float32)
    return i, q


def _wait(rec):
    rec._q.join()          # recorder calls task_done() per item


def _read24(path):
    w = wave.open(path, 'rb')
    assert w.getnchannels() == 2 and w.getsampwidth() == 3 and w.getframerate() == RATE
    raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.uint8).reshape(-1, 3)
    v = (raw[:, 0].astype(np.int32) | (raw[:, 1].astype(np.int32) << 8)
         | (raw[:, 2].astype(np.int32) << 16))
    v = np.where(v >= 2**23, v - 2**24, v)
    return v[0::2], v[1::2]


def test_pack_24bit_exact_and_16bit():
    i = np.array([0, 1, -1, 2**23 - 1, -2**23], dtype=np.float32)
    q = -i
    b = R.pack_iq(i, q, 24)
    assert len(b) == 2 * len(i) * 3
    b16 = R.pack_iq(i, q, 16)
    v = np.frombuffer(b16, dtype='<i2')
    assert v[6] == 32767 and v[8] == -32768        # full scale maps to 16-bit full scale


def test_record_append_gap_and_stop(caplog=None):
    rng = np.random.default_rng(1)
    with tempfile.TemporaryDirectory() as d:
        rec = R.BandRecorder(d, bits=24, rate=RATE)
        i1, q1 = _swap(rng, 600)
        rec.submit(7090, 100.0, i1, q1, 600)          # not recording yet: no RECORD file
        _wait(rec)
        assert not glob.glob(os.path.join(d, '*.wav'))
        open(os.path.join(d, 'RECORD'), 'w').close()
        i2, q2 = _swap(rng, 600)
        i3, q3 = _swap(rng, 400)
        rec.submit(7090, 200.0, i2, q2, 600)
        rec.submit(7090, 200.6, i3, q3, 400)          # contiguous (200.0 + 600/1000)
        rec.submit(14090, 200.0, i3, q3, 400)
        _wait(rec)
        os.remove(os.path.join(d, 'RECORD'))
        rec.submit(7090, 201.0, i1, q1, 600)           # inactive -> closes session
        _wait(rec)
        files = sorted(glob.glob(os.path.join(d, '*.wav')))
        assert len(files) == 2 and any('_7090kHz_' in f for f in files)
        gi, gq = _read24([f for f in files if '_7090kHz_' in f][0])
        assert np.array_equal(gi, np.concatenate([i2, i3]).astype(np.int32))   # bit-exact
        assert np.array_equal(gq, np.concatenate([q2, q3]).astype(np.int32))


def test_gap_is_logged():
    rng = np.random.default_rng(2)
    seen = []

    class H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())
    R.log.addHandler(H())
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, 'RECORD'), 'w').close()
        rec = R.BandRecorder(d, bits=24, rate=RATE)
        i, q = _swap(rng, 500)
        rec.submit(7090, 10.0, i, q, 500)
        rec.submit(7090, 12.0, i, q, 500)              # expected 10.5 -> 1.5 s gap
        _wait(rec)
        rec.close()
    assert any('gap' in m and '7090' in m for m in seen), seen


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    ok = 0
    for fn in fns:
        try:
            fn(); print('PASS ', fn.__name__); ok += 1
        except Exception as e:
            print('FAIL ', fn.__name__, type(e).__name__, e)
    print(f'\n{ok}/{len(fns)} passed'); sys.exit(0 if ok == len(fns) else 1)
