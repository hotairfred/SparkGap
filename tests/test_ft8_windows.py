"""FT8 windows from the snapshot's start time: two around each slot, sliding when the time is unknown."""

import pytest

import sparkgap

RATE, N = 12000, 60 * 12000          # a 60 s snapshot at 12 kHz
WIN = 15 * RATE
TX_S = 12.64                          # an FT8 transmission: 79 symbols of 0.16 s


def test_a_snapshot_from_quarter_past_the_minute() -> None:
    # first sample at hh:mm:00.25, as skimmer1's swaps land
    starts = sparkgap.ft8_window_starts(1792551360.25, N)
    assert starts == [0, 3000, 171000, 183000, 351000, 363000, 531000]
    assert all(k + WIN <= N for k in starts)


def test_unknown_time_keeps_the_sliding_windows() -> None:
    assert sparkgap.ft8_window_starts(0.0, N) == list(range(0, 46 * RATE, RATE))


@pytest.mark.parametrize("frac", [0.0, 0.05, 0.25, 0.31, 0.9, 7.5, 14.9, 59.8])
def test_every_whole_transmission_is_inside_a_window(frac: float) -> None:
    t0 = 1792551300.0 + frac
    starts = [k / RATE for k in sparkgap.ft8_window_starts(t0, N)]
    slot = (-t0) % 15 - 15
    while slot < 60:
        tx0 = slot + 0.5                                   # nominal start, DT 0
        if tx0 >= 0 and tx0 + TX_S <= 60:
            assert any(s <= tx0 and s + 15 >= tx0 + TX_S for s in starts), (frac, slot)
        slot += 15


def test_eight_or_fewer_windows_a_minute() -> None:
    for frac in (0.25, 3.3, 9.9):
        assert len(sparkgap.ft8_window_starts(1792551300.0 + frac, N)) <= 8
