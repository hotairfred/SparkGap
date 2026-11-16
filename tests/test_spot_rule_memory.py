"""spot_rule_memory: the re-spot table survives a restart, so a station spotted minutes before
it is not spotted again; entries older than RESPOT_S are dropped."""
import json
import types

import sparkgap
import spot_rule
from spot_rule import RESPOT_S, RepeatSpotRule

CQ = "CQ CQ DE K1ABC K1ABC K"


def _rule() -> RepeatSpotRule:
    return RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True)


def _feed(rule: RepeatSpotRule, t0: float, n: int = 2) -> list:
    out = []
    for k in range(n):
        now = t0 + 60 * k
        out += rule.feed(rule.window_id(1, int(now), now), 7030.0, CQ, now)
    return out


def test_a_restart_without_memory_spots_again_and_with_memory_does_not() -> None:
    first = _rule()
    assert [c for c, _ in _feed(first, 1000.0)] == ["K1ABC"]
    saved = first.last_spots()
    assert [c for c, _ in _feed(_rule(), 1200.0)] == ["K1ABC"]        # fresh rule: spotted again
    again = _rule()
    assert again.restore_last_spots(saved, 1200.0) == 1
    assert _feed(again, 1200.0) == []                                   # remembered: no re-spot


def test_old_and_bad_entries_are_dropped() -> None:
    rule = _rule()
    rows = [["K1ABC", 7, 0.0, 7030.0], ["W1AW", 7, 300.0, 7040.0], ["bad"], ["K2X", "x", 1, 2]]
    assert rule.restore_last_spots(rows, RESPOT_S + 1.0) == 1
    assert [r[0] for r in rule.last_spots()] == ["W1AW"]


def test_save_and_load_through_sparkgap(tmp_path) -> None:
    path = str(tmp_path / "spot_memory.json")
    a = types.SimpleNamespace(spot_rule=_rule(), local_rule=_rule())
    _feed(a.spot_rule, 1000.0)
    sparkgap._save_spot_memory(path, a)
    with open(path) as f:
        assert set(json.load(f)) == {"rbn", "local"}
    b = types.SimpleNamespace(spot_rule=_rule(), local_rule=None)
    assert sparkgap._load_spot_memory(path, b, 1300.0) == 1
    assert sparkgap._load_spot_memory(str(tmp_path / "none.json"), b, 1300.0) == 0
    sparkgap._save_spot_memory(None, a)                                  # off: nothing written


def test_module_constant_is_ten_minutes() -> None:
    assert spot_rule.REPEAT_S == 600.0


def test_respot_waits_ten_minutes_as_rbn_times_them() -> None:
    """RBN stamps a spot when the upload reaches it, seconds after ours: a re-spot at exactly 10
    minutes by our clock showed as 9 min 51 s there (K3PR, Z68MU on 11-06). Wait the margin too."""
    rule = _rule()
    assert [c for c, _ in _feed(rule, 1000.0)] == ["K1ABC"]             # spotted at 1060
    assert _feed(rule, 1060.0 + spot_rule.REPEAT_S - 60.0) == []          # copies to 1600, no re-spot
    assert [c for c, _ in _feed(rule, 1060.0 + spot_rule.RESPOT_S)] == ["K1ABC"]


def test_a_call_on_two_frequencies_is_not_re_spotted_at_each_switch() -> None:
    """WAE RTTY 11-14/15: a call read on two frequencies of a band (K4NMR on 14084.2 and 14100.5) was re-spotted
    each time it switched between them, the old frequency forgotten: all of W3RGA's dupes those days."""
    rule = _rule()
    spots = []
    for k, f in enumerate([14084.2, 14100.5, 14084.2, 14100.5, 14084.2]):
        now = 1000.0 + 60 * k
        spots += rule.feed(rule.window_id(int(f * 10), int(now), now), f, "CQ CQ DE K1ABC K1ABC K", now)
        spots += rule.feed(rule.window_id(int(f * 10), int(now) + 1, now + 1), f, "CQ CQ DE K1ABC K1ABC K", now + 1)
    assert spots == [("K1ABC", 14084.2), ("K1ABC", 14100.5)]          # each frequency once
    rows = rule.last_spots()
    assert sorted(r[3] for r in rows if r[0] == "K1ABC") == [14084.2, 14100.5]   # memory keeps both
    b = _rule()
    assert b.restore_last_spots(rows, 1400.0) == 2
    assert b.feed(b.window_id(1, 1400, 1400.0), 14100.5, "CQ CQ DE K1ABC K1ABC K", 1400.0) == []
    later = 1000.0 + RESPOT_S + 300
    again = rule.feed(rule.window_id(14084, int(later), later), 14084.2, "CQ CQ DE K1ABC K1ABC K", later)
    again += rule.feed(rule.window_id(14084, int(later) + 1, later + 1), 14084.2, "CQ CQ DE K1ABC K1ABC K", later + 1)
    assert again == [("K1ABC", 14084.2)]                               # 10 minutes on: re-spotted as before
