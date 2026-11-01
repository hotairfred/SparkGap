"""itila2_parallel_decode keeps memory flat: the decode pool's threads live with their scanner and
each thread's decoder scratch is freed when it exits (cdub89/SparkGap#35)."""

from pathlib import Path

import pytest

from tests.test_itila2_parallel_decode import DEC, SC, _run

GROWTH_MB_MAX = 16.0  # over four runs after warm-up; the leak guarded against grew 13 MB per run (52 MB here).
# 4 MB was too tight inside the full suite on CI's 3.12 runner (+4.6 MB twice, flat on its own: +0.3 MB in 12 runs)


def _rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    raise AssertionError("no VmRSS")


@pytest.mark.skipif(not (Path(DEC).exists() and Path(SC).exists()),
                    reason="libitila2 libraries not built")
def test_repeated_scanners_with_parallel_decode_do_not_grow() -> None:
    for _ in range(2):          # warm-up: the C library's per-thread arenas settle
        _run(4, True)
    before = _rss_mb()
    for _ in range(4):
        _run(4, True)
    assert _rss_mb() - before < GROWTH_MB_MAX
