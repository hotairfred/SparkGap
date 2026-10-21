"""FT8 DT against the slot, and the warning when decodes say the PC clock is off."""

import sparkgap

MIN = 1792551300.0                     # a wall-clock minute


def test_dt_counts_from_the_slot_plus_half_a_second() -> None:
    # window from :44.5 (0.5 s before the :45 slot), decode_ft8 time 1.2 s: started :45.7 -> DT +0.2
    assert abs(sparkgap.ft8_slot_dt(MIN + 44.5, 1.2) - 0.2) < 1e-6
    assert abs(sparkgap.ft8_slot_dt(MIN + 0.25, 0.25) - 0.0) < 1e-6
    # across the minute: window from :59.75, time 0.9 -> started :00.65 -> DT +0.15
    assert abs(sparkgap.ft8_slot_dt(MIN + 59.75, 0.9) - 0.15) < 1e-6
    # a transmission 1 s early (window from :14.5, time 0) shows as -1.0, not +14.0
    assert abs(sparkgap.ft8_slot_dt(MIN + 14.5, 0.0) - (-1.0)) < 1e-6


def test_no_warning_while_the_clock_is_right() -> None:
    w = sparkgap.Ft8ClockWatch()
    for m in range(10):
        med, warn = w.add([0.1, 0.2, 0.3, 0.2, 0.25, -0.1], MIN + 60 * m)
        assert warn is None and abs(med - 0.2) < 0.05


def test_a_clock_two_seconds_slow_warns_once_per_ten_minutes() -> None:
    w = sparkgap.Ft8ClockWatch()
    warns = [w.add([1.9, 2.0, 2.1, 2.0, 2.2], MIN + 60 * m)[1] for m in range(20)]
    fired = [m for m, x in enumerate(warns) if x]
    assert fired and fired[0] < 6 and len(fired) == 2 and fired[1] - fired[0] == 10
    assert "+2.0 s" in warns[fired[0]]


def test_cycles_with_few_decodes_are_left_out() -> None:
    w = sparkgap.Ft8ClockWatch()
    for m in range(10):
        med, warn = w.add([3.0, 3.0], MIN + 60 * m)        # two decodes a cycle: not enough to judge
        assert med is None and warn is None
