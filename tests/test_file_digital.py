"""File mode --digital: the FT8/RTTY minute cycle runs on the recording at each minute of audio time,
with the last 60 s of the band and the same job the live receiver hands it; RTTY exclusions reach the
file's CW scanner, and the cycle's clock is audio time."""

import numpy as np

import sparkgap
from sparkgap import _FileDigital

FS = _FileDigital.FS


class _Sc:
    def __init__(self) -> None:
        self.zones = []

    def exclude(self, lo, hi, hold):
        self.zones.append((lo, hi, hold))
        return True


class _Scanner:
    def __init__(self) -> None:
        self._sc = _Sc()


class _Manager:
    def __init__(self) -> None:
        self._itila_scanner = _Scanner()


def _run(monkeypatch, seconds: float, t0: float = 1_762_610_340.0):   # 2025-11-08 13:59:00Z
    jobs = []
    monkeypatch.setattr(sparkgap, "_ft8_cycle", lambda j, sk: jobs.append((j, sk.clock())))
    d = _FileDigital({"enable_ft8": True, "enable_rtty": True}, None, _Manager(), 14090.0, t0)
    block = FS // 10
    for k in range(int(seconds * 10)):
        x = np.full(block, float(k), dtype=np.float32)          # each block's samples hold its index
        d.add(x, -x, k / 10)
    return d, jobs


def test_one_job_per_minute_of_audio(monkeypatch) -> None:
    d, jobs = _run(monkeypatch, 150)                 # 13:59:00 .. 14:01:30
    assert len(jobs) == 2                            # the minutes ending 14:00:00 and 14:01:00
    (job,), clock = jobs[0]
    _bn, _ri, ck, dial, fi, fq, n, t_first = job
    assert (ck, dial, n) == (14090.0, 14074, 60 * FS)
    assert t_first == d.t0 and clock == d.t0 + 60.0  # the cycle sees audio time at the boundary
    assert fi[0] == 0 and fi[-1] == 599 and fq[-1] == -599
    (job2,), _ = jobs[1]
    assert job2[7] == d.t0 + 60 and job2[4][0] == 600


def test_a_short_first_minute_is_skipped(monkeypatch) -> None:
    _, jobs = _run(monkeypatch, 80, t0=1_762_610_370.0)   # starts 13:59:30: 30 s, then a full minute
    assert len(jobs) == 0                                  # the 30 s minute is short; the next not ended


def test_rtty_exclusion_reaches_the_file_scanner(monkeypatch) -> None:
    d, _ = _run(monkeypatch, 1)
    assert d._exclude_cw(d.bn, 14080000.0, 14080400.0, 120.0)
    assert d.managers[0]._itila_scanner._sc.zones == [(14080000.0, 14080400.0, 120.0)]


def test_flag_parses() -> None:
    import inspect
    assert "'--digital'" in inspect.getsource(sparkgap.main)
