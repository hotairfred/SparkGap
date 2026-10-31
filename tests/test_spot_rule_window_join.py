"""Window join (spot_rule_window_join, cdub89/SparkGap#37): a CQ group cut by the decode window
edge, CQ CQ SKCC DE at the end of one window and the call at the start of the next (lake4, 20 m,
2026-10-23, K2MJP), counts as one group in the next window."""

from spot_rule import RepeatSpotRule

FREQ = 14051.0
WINDOW_S = 30.0


def _tier2(call: str) -> int:
    del call
    return 2


def _rule(join: bool = True) -> RepeatSpotRule:
    return RepeatSpotRule(_tier2, cq_groups=True, lockon=True, cq_qualifiers=True,
                          window_s=WINDOW_S, window_join=join)


def _feed(rule: RepeatSpotRule, texts: tuple[str, ...], bins: tuple[int, ...] | None = None,
          times: tuple[float, ...] | None = None) -> list[tuple[str, float]]:
    spots: list[tuple[str, float]] = []
    for k, text in enumerate(texts):
        now = times[k] if times else k * WINDOW_S
        bin_id = bins[k] if bins else 1
        spots += rule.feed(rule.window_id(bin_id, k, now), FREQ, text, now)
    return spots


CUT = ("QRL? CQ CQ SKCC DE", "K2MJP K2MJP K", "CQ CQ SKCC DE K2MJP")


def test_the_call_after_a_cut_group_counts_as_a_copy() -> None:
    assert _feed(_rule(), CUT) == [("K2MJP", FREQ)]


def test_without_the_option_the_cut_window_gives_no_copy() -> None:
    assert _feed(_rule(join=False), CUT) == []


def test_a_window_not_ending_with_de_leaves_no_tail() -> None:
    assert _feed(_rule(), ("QRL? CQ CQ SKCC", "K2MJP K2MJP K", "CQ CQ SKCC DE K2MJP")) == []


def test_de_without_a_cq_before_it_leaves_no_tail() -> None:
    assert _feed(_rule(), ("N2YC N2YC DE", "K2MJP K2MJP K", "CQ CQ SKCC DE K2MJP")) == []


def test_the_tail_stays_with_its_own_bin() -> None:
    assert _feed(_rule(), CUT, bins=(1, 2, 2)) == []


def test_a_late_window_does_not_join() -> None:
    assert _feed(_rule(), CUT, times=(0.0, 50.0, 80.0)) == []


def test_both_paths_of_the_next_window_read_the_tail_once() -> None:
    rule = _rule()
    spots: list[tuple[str, float]] = []
    spots += rule.feed(rule.window_id(1, 0, 0.0), FREQ, "QRL? CQ CQ SKCC DE", 0.0)
    spots += rule.feed(rule.window_id(1, 0, 0.0), FREQ, "QRL? CQ CQ SKCC DE", 0.0)
    w1 = rule.window_id(1, 1, 30.0)
    spots += rule.feed(w1, FREQ, "K2MJP K2MJP K", 30.0)
    spots += rule.feed(w1, FREQ, "K2MJP K2MJP", 30.0)
    assert spots == []                     # one window, one copy: no spot yet at tier 2
    assert sum(1 for c in rule._copies if c.call == "K2MJP") == 1
    spots += rule.feed(rule.window_id(1, 2, 60.0), FREQ, "CQ CQ SKCC DE K2MJP", 60.0)
    assert spots == [("K2MJP", FREQ)]


def test_the_two_paths_of_one_window_never_join_each_other() -> None:
    rule = _rule()
    w0 = rule.window_id(1, 0, 0.0)
    assert rule.feed(w0, FREQ, "QRL? CQ CQ SKCC DE", 0.0) == []
    assert rule.feed(w0, FREQ, "K2MJP K2MJP K", 0.0) == []
    assert not any(c.call == "K2MJP" for c in rule._copies)


def test_the_config_key_reaches_the_rule() -> None:
    import sparkgap
    tracker = sparkgap.SpotTracker(set(), set())
    assert not sparkgap._make_spot_rule({}, tracker).window_join
    assert sparkgap._make_spot_rule({"spot_rule_window_join": True}, tracker).window_join


def test_a_joined_window_never_promotes_a_caller_against_the_owner() -> None:
    # lake3 20 m 2026-10-30 14043.0: K4NYM's CQ cut after DE, his call lost, N9ZI answering him
    rule = _rule()
    owned = ("CQ CQ POTA DE K4NYM K4NYM K", "CQ CQ POTA DE K4NYM K", "CQ CQ POTA DE")
    assert _feed(rule, owned) == [("K4NYM", FREQ)]
    assert rule.feed(rule.window_id(1, 3, 90.0), FREQ, "N9ZI N9ZI N9ZIDE ?NYM BK HI", 90.0) == []
    assert not any(c.call == "N9ZI" for c in rule._copies)
    # the owner's own call after the cut still joins
    assert rule.feed(rule.window_id(1, 4, 120.0), FREQ, "CQ CQ POTA DE", 120.0) == []
    rule.feed(rule.window_id(1, 5, 150.0), FREQ, "K4NYM K4NYM K", 150.0)
    assert sum(1 for c in rule._copies if c.call == "K4NYM" and c.window == 5) == 1


def test_a_group_that_already_names_a_call_leaves_no_tail() -> None:
    # CQ N1ABC TEST DE counted N1ABC where it was; a replay would copy him again
    rule = _rule()
    assert _feed(rule, ("CQ N1ABC TEST DE", "K2MJP K2MJP K", "CQ CQ SKCC DE K2MJP")) == []
    assert sum(1 for c in rule._copies if c.call == "N1ABC") == 1


def test_only_the_next_window_reads_the_tail() -> None:
    # windows cut at silence can come closer than a window length: the one between consumes it
    assert _feed(_rule(), ("QRL? CQ CQ SKCC DE", "E E T", "K2MJP K2MJP K", "CQ CQ SKCC DE K2MJP"),
                 times=(0.0, 10.0, 20.0, 50.0)) == []


def test_a_reused_bin_id_on_another_frequency_does_not_join() -> None:
    rule = _rule()
    assert rule.feed(rule.window_id(1, 0, 0.0), FREQ, "QRL? CQ CQ SKCC DE", 0.0) == []
    w1 = rule.window_id(1, 1, 30.0)        # the bin id reused by a new bin 3 kHz away
    assert rule.feed(w1, FREQ + 3.0, "K2MJP K2MJP K", 30.0) == []
    assert not any(c.call == "K2MJP" for c in rule._copies)


def test_a_runner_cut_at_every_window_end_joins_on_both_paths() -> None:
    # K2MJP calls CQ every 20 s in 30 s windows: each window ends with a cut CQ CQ DE on both paths
    rule = _rule()
    w0 = rule.window_id(1, 0, 0.0)
    for _ in range(2):
        assert rule.feed(w0, FREQ, "QRL? CQ CQ SKCC DE", 0.0) == []
    w1 = rule.window_id(1, 1, 30.0)
    for _ in range(2):
        assert rule.feed(w1, FREQ, "K2MJP K2MJP K CQ CQ SKCC DE", 30.0) == []
    assert sum(1 for c in rule._copies if c.call == "K2MJP") == 1
    w2 = rule.window_id(1, 2, 60.0)
    assert rule.feed(w2, FREQ, "K2MJP K2MJP K", 60.0) == [("K2MJP", FREQ)]
