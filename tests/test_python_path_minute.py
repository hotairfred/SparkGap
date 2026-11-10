"""FT8 and RTTY on the Python receive path (RTP from radiod, Flex): each band keeps its minute of IQ
(_py_minute_add) and the minute boundary hands the last 60 s to the FT8/RTTY cycle (_py_minute_tick),
as the C receiver's swap buffer does for the Red Pitaya; ft8_dial_khz finds the FT8 dial for any centre."""
import types

import numpy as np

import sparkgap
from sparkgap import SparkGap, ft8_dial_khz

FS = 192000


def test_the_red_pitaya_centres_keep_their_dials() -> None:
    for ck, dial in sparkgap.FT8_CENTRE_DIALS.items():
        assert ft8_dial_khz(ck) == dial


def test_band_names_and_other_centres_find_the_dial_in_their_span() -> None:
    assert ft8_dial_khz(7076.8) == 7074            # 40m by name at 192 kHz
    assert ft8_dial_khz(1886.8) == 1840            # 160m
    assert ft8_dial_khz(18144.8) == 18100          # 17m
    assert ft8_dial_khz(7030.0, fs=48000) is None  # 7008-7052: no dial
    assert ft8_dial_khz(10125.0, fs=48000) == 10136


def _buf() -> dict:
    return {'min_i': [], 'min_q': [], 'min_n': 0, 'min_t0': 0.0}


def test_the_minute_buffer_keeps_at_most_65_seconds(monkeypatch) -> None:
    monkeypatch.setattr(sparkgap.time, 'time', lambda: 1000.0)
    buf, chunk = _buf(), np.ones(FS, dtype=np.float64)
    for _ in range(70):
        SparkGap._py_minute_add(None, buf, chunk, chunk, FS)
    assert buf['min_n'] == 65 * FS and len(buf['min_i']) == 65
    assert buf['min_i'][0].dtype == np.float32
    assert buf['min_t0'] == 1000.0 - 1 + 5          # first chunk ended at 1000 s; 5 s dropped


def _skimmer(bands):
    sk = types.SimpleNamespace(_band_meta=[], managers=[], _band_bufs={}, _ft8_running=False, _ft8_prev_sec=59.0)
    for ri, (name, centre_hz, seconds) in enumerate(bands):
        sk._band_meta.append((name, centre_hz, ri))
        sk.managers.append(None)
        b = _buf()
        if seconds:
            b['min_i'] = [np.arange(seconds * FS, dtype=np.float32)]
            b['min_q'] = [np.zeros(seconds * FS, dtype=np.float32)]
            b['min_n'], b['min_t0'] = seconds * FS, 940.0
        sk._band_bufs[ri] = b
    return sk


def test_the_minute_boundary_hands_the_last_60_seconds_to_the_cycle(monkeypatch) -> None:
    jobs = []
    monkeypatch.setattr(sparkgap, '_ft8_decode', lambda j, skimmer: jobs.extend(j))
    monkeypatch.setattr(sparkgap.time, 'time', lambda: 1020.5)      # 0.5 s past a minute
    sk = _skimmer([('40m', 7076800, 62), ('30m', 10176800, 30), ('160m', 1886800, 61)])
    SparkGap._py_minute_tick(sk)
    for t in list(sparkgap.threading.enumerate()):
        if t.name != 'MainThread' and t.daemon:
            t.join(timeout=1)
    assert [(j[0], j[3], j[6]) for j in jobs] == [('40m', 7074, 60 * FS), ('160m', 1840, 60 * FS)]
    bn, ri, ck, dial, fi, fq, n, t0 = jobs[0]
    assert fi[0] == 2 * FS and t0 == 942.0           # the last 60 of 62 s, and its first sample's time
    assert sk._ft8_running
    assert all(b['min_n'] == 0 for b in sk._band_bufs.values())    # every band starts its next minute


def test_no_cycle_away_from_the_boundary_or_while_one_runs(monkeypatch) -> None:
    jobs = []
    monkeypatch.setattr(sparkgap, '_ft8_decode', lambda j, skimmer: jobs.extend(j))
    monkeypatch.setattr(sparkgap.time, 'time', lambda: 1030.0)      # mid-minute
    sk = _skimmer([('40m', 7076800, 62)])
    SparkGap._py_minute_tick(sk)
    assert sk._band_bufs[0]['min_n'] == 62 * FS
    monkeypatch.setattr(sparkgap.time, 'time', lambda: 1020.5)
    sk._ft8_prev_sec, sk._ft8_running = 59.0, True
    SparkGap._py_minute_tick(sk)
    assert jobs == [] and sk._band_bufs[0]['min_n'] == 62 * FS
