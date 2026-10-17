"""Repeat-evidence spot rule (spot_rule.py). Text cases are decodes from the 2026-09-23 replays."""

import pytest

from spot_rule import LOCK_IDLE_S, REPEAT_S, RepeatSpotRule, _regap, runner_calls, runner_groups, split_runs

SCP = {"K0TQ", "WX7V", "W1AW", "VE7KW", "K3JT", "K1ABC", "K1AJ", "AA3B", "NA2U"}


def _tier2(call: str) -> int:
    del call
    return 2


def _rule() -> RepeatSpotRule:
    return RepeatSpotRule(_tier2)


def test_call_right_after_keyword() -> None:
    assert runner_calls("CQ CWT K0TQ") == ["K0TQ"]
    assert runner_calls("TU CWT NA2U") == []


def test_words_between_keyword_and_call() -> None:
    assert runner_calls("CQ POTA DE WX7V WX7V K") == ["WX7V"]
    assert runner_calls("CQ CQ CQ DE W1AW") == ["W1AW"]


def test_merged_keyword_is_split() -> None:
    assert runner_calls("CQK1ABC K1ABC") == ["K1ABC"]


def test_split_runs_splits_words_run_together() -> None:
    # hand-sent CW, PA QSO Party 10-10: several words with no gap between them
    assert split_runs("CQCQDE WA8VTD") == "CQ CQ DE WA8VTD"
    assert split_runs("CQCQWESDE K4OB K4OB K") == "CQ CQ WES DE K4OB K4OB K"
    assert split_runs("CQPADEW3RA/I NN") == "CQ PA DE W3RA/I NN"
    assert split_runs("CQSKCCDEKD3CNH") == "CQ SKCC DE KD3CNH"


def test_split_runs_keeps_calls_and_other_words() -> None:
    for text in ("WA8VTD", "K4OBK", "TEST", "5NN EDMU", "EEEE"):
        assert split_runs(text) == text
    assert split_runs("CQPW6KK") == "CQ PW6KK"     # as tokens() reads it: CQP W6KK is ambiguous


def test_split_runs_key() -> None:
    """Off by default: CQCQDE names no sender, as before."""
    texts = ["CQCQDE WA8VTD WA8VTD K", "CQCQDE WA8VTD WA8VTD K"]
    assert _feed(_rule(), texts) == []
    assert _feed(RepeatSpotRule(_tier2, split_runs=True), texts) == [("WA8VTD", 7019.3)]


def test_caller_sent_the_exchange_does_not_count() -> None:
    assert runner_calls("TEST AA3B FRED 2455") == []
    assert runner_calls("TEST VE7KW TEST K3JT K3JT KEITS 2182") == ["VE7KW"]


def test_cq_qualifier_is_not_an_exchange() -> None:
    def q(text: str) -> list[str]:
        return runner_calls(text, every=True, qualifiers=True)
    # 20m 2026-10-11, PA QSO Party and WES: the qualifier after the call read as name + state
    assert q("CQ PA WB3EML PA CQ PA WB3EML PA") == ["WB3EML", "WB3EML"]
    assert q("CQ FD CQ FD DE WX7V FD CQ FD CQ FD DE WX7V FD") == ["WX7V", "WX7V"]
    assert q("CQ WES DE WX7V WX7V WES TX CQ WES DE WX7V WX7V WES TX") == ["WX7V", "WX7V"]
    # qualifier only after the call: the keyword after it is no state
    assert q("CQ CQ DE WX7V WX7V FD CQ CQ DE WX7V WX7V FD") == ["WX7V", "WX7V"]
    # a caller sent a real exchange still does not count
    assert q("CQ CWT W6YH TEST K1ABC BOB MA") == ["W6YH"]
    assert q("CQ PA W6YH TEST K1ABC BOB 123") == ["W6YH"]
    # known cost: an exchange whose number was lost before a keyword reads as a CQ group
    # (1 copy in 32,000 windows on four recordings); the repeat count still has to be met
    assert q("TEST K1ABC BOB CQ") == ["K1ABC"]


def test_cq_qualifiers_key() -> None:
    """Off by default: PA CQ after the call reads as name and state, one copy per window."""
    texts = ["CQ PA WB3EML PA CQ PA WB3EML PA"]
    assert _feed(RepeatSpotRule(_tier2, cq_groups=True), texts) == []
    assert _feed(RepeatSpotRule(_tier2, cq_groups=True, cq_qualifiers=True),
                 texts) == [("WB3EML", 7019.3)]


def test_contest_report_and_number_is_an_exchange() -> None:
    # Morse Runner CQ WW cells (hotairfred, cdub89/SparkGap#8): the caller follows the CQ
    assert runner_calls("CQ W6YH TEST EI4KF 5NN 4 5NN 4 TU") == ["W6YH"]
    assert runner_calls("CQ W6YH TEST 2E0FOR 5NN 4 A TU") == ["W6YH"]
    assert runner_calls("TEST K1ABC 599 TT5") == []


def test_every_cq_form_names_the_runner() -> None:
    # Fred (cdub89/SparkGap#8): runners mix these forms within one run
    for form in ("TEST W6YH", "CQ TEST W6YH", "CQ W6YH TEST", "CQ TEST W6YH TEST"):
        assert runner_calls(form) == ["W6YH"], form


def test_call_sent_twice_before_the_closing_keyword() -> None:
    assert runner_calls("CQ TEST W6YH W6YH TEST EI4KF") == ["W6YH"]
    assert runner_calls("CQ W6YH W6YH TEST EI4KF") == ["W6YH"]
    assert runner_calls("CQ TEST W6YH W6YH") == ["W6YH"]


def test_tu_and_a_bare_trailing_test_do_not_name_the_call() -> None:
    # measured on two CWTs: TU <call> is mostly said to callers
    assert runner_calls("TU W6YH") == []
    assert runner_calls("W6YH TEST") == []
    assert runner_calls("TU W6YH TEST") == []


def test_test_after_an_unnamed_call_opens_a_group_on_the_next_call() -> None:
    # known limit: TU W6YH TEST EI4KF (caller) reads like TU K1ABC TEST W6YH (runner signs)
    assert runner_calls("TU W6YH TEST EI4KF") == ["EI4KF"]
    assert runner_calls("TU W6YH TEST EI4KF 5NN 4") == []
    assert runner_calls("TU K1ABC TEST W6YH") == ["W6YH"]


def test_a_call_after_the_closing_keyword_is_the_next_transmission() -> None:
    assert runner_calls("CQ W6YH TEST EI4KF 5NN 4") == ["W6YH"]
    # the second TEST closes the first group, so K3WW opens nothing
    assert runner_calls("TEST K3AW TEST K3WW TEST") == ["K3AW"]
    assert runner_calls("TEST VE7KW TEST K3JT K3JT KEITS 2182") == ["VE7KW"]


def test_keyword_closing_a_cq_opens_no_lookahead() -> None:
    assert runner_calls("CQ W6YH TEST K1ABC") == ["W6YH"]
    assert runner_calls("CQ TEST K1ABC") == ["K1ABC"]


def test_the_station_being_called_is_not_the_runner() -> None:
    # 20m 2026-10-13: N8EM calling S52Y; S52Y was spotted
    assert runner_calls("CQ S52Y DE N8EM K") == []
    assert runner_calls("CQ S52Y S52Y DE N8EM N8EM K") == []      # N8EM is calling, not running
    assert runner_calls("CQ DE N8EM N8EM K") == ["N8EM"]


def test_caller_echoing_the_runner_keeps_the_runners_copy() -> None:
    # hotairfred/SparkGap#52 review: after TEST or DE the call is the runner's, not one being called
    assert runner_calls("CQ CQ DE K1ABC K1ABC K1ABC DE N2XX N2XX K") == ["K1ABC"]
    assert runner_calls("CQ TEST K1ABC DE N2XX") == ["K1ABC"]
    assert runner_calls("CQ TEST W1AW DE W1AWX") == ["W1AW"]
    assert runner_calls("CQ K4DH DE K4DH K") == ["K4DH"]


def test_no_keyword_no_runner() -> None:
    assert runner_calls("DE K7GUD NICE MEET U RICK") == []


def test_spot_after_tier_windows() -> None:
    r = _rule()
    assert r.feed(r.window_id(1, 0, 0.0), 14030.0, "CQ CWT K0TQ", 0.0) == []
    assert r.feed(r.window_id(1, 1, 60.0), 14030.0, "CQ CWT K0TQ", 60.0) == [("K0TQ", 14030.0)]


def test_both_lpf_paths_of_one_window_count_once() -> None:
    r = _rule()
    w = r.window_id(1, 0, 0.0)
    assert r.feed(w, 14030.0, "CQ CWT K0TQ", 0.0) == []
    assert r.feed(r.window_id(1, 0, 0.0), 14030.0, "CQ CWT K0TQ", 0.0) == []


def test_a_second_bin_on_the_same_audio_is_no_copy() -> None:
    # AK4W for AK4JK, 2026-10-13: bins on 14007.1 and 14007.0 gave the same text 36 s apart
    text = "CQ CQ CQDEAK4W IIAK4JK AN AK4JK"
    r = RepeatSpotRule(_tier2, split_runs=True)
    assert r.feed(r.window_id(1, 0, 0.0), 14007.1, text, 0.0) == []
    assert r.feed(r.window_id(2, 0, 36.0), 14007.0, text, 36.0) == []
    assert r.feed(r.window_id(1, 1, 60.0), 14007.1, text, 60.0) == [("AK4W", 14007.1)]   # sent again
    r = RepeatSpotRule(_tier2, split_runs=True, window_s=30.0)
    assert r.feed(r.window_id(1, 0, 0.0), 14007.1, text, 0.0) == []
    assert r.feed(r.window_id(2, 0, 36.0), 14007.0, text, 36.0) == [("AK4W", 14007.1)]


def test_every_cq_group_is_listed_on_request() -> None:
    text = "CQ CQ WES WES DE N5QLZ N5QLZ K CQ CQ WES WES DE N5QLZ"
    assert runner_calls(text) == ["N5QLZ"]
    assert runner_calls(text, every=True) == ["N5QLZ", "N5QLZ"]
    assert runner_calls("CQ TEST W6YH W6YH TEST", every=True) == ["W6YH"]


def test_cq_groups_spot_in_the_first_window() -> None:
    """40m WES 2026-10-11: N5QLZ sent two CQs in one window and waited 7 minutes for a second."""
    text = "CQ CQ WES WES DE N5QLZ N5QLZ K CQ CQ WES WES DE N5QLZ"
    off, on = _rule(), RepeatSpotRule(_tier2, cq_groups=True)
    assert off.feed(off.window_id(1, 0, 0.0), 7050.2, text, 0.0) == []
    assert on.feed(on.window_id(1, 0, 0.0), 7050.2, text, 0.0) == [("N5QLZ", 7050.2)]


def test_cq_groups_do_not_add_the_two_lpf_paths() -> None:
    r = RepeatSpotRule(lambda c: 3, cq_groups=True)
    text = "CQ WES DE N5QLZ N5QLZ K CQ WES DE N5QLZ"
    assert r.feed(r.window_id(1, 0, 0.0), 7050.2, text, 0.0) == []
    assert r.feed(r.window_id(1, 0, 0.0), 7050.2, text, 0.0) == []
    assert r.feed(r.window_id(1, 1, 60.0), 7050.2, "CQ WES DE N5QLZ N5QLZ", 60.0) == [("N5QLZ", 7050.2)]


def test_cq_groups_take_the_larger_count_of_the_two_lpf_paths() -> None:
    one, three = "CQ WES DE N5QLZ N5QLZ", "CQ N5QLZ K CQ N5QLZ K CQ N5QLZ"
    for first, second, want in ((one, three, [[], [("N5QLZ", 7050.2)]]), (three, one, [[("N5QLZ", 7050.2)], []])):
        r = RepeatSpotRule(lambda c: 3, cq_groups=True)
        w = r.window_id(1, 0, 0.0)
        assert [r.feed(w, 7050.2, first, 0.0), r.feed(w, 7050.2, second, 0.0)] == want


def test_cq_groups_copies_expire() -> None:
    r = RepeatSpotRule(lambda c: 3, cq_groups=True)
    text = "CQ WES DE N5QLZ N5QLZ K CQ WES DE N5QLZ"
    assert r.feed(r.window_id(1, 0, 0.0), 7050.2, text, 0.0) == []
    late = REPEAT_S + 61.0
    assert r.feed(r.window_id(1, 9, late), 7050.2, "CQ WES DE N5QLZ N5QLZ", late) == []
    w = r.window_id(1, 10, 0.0)
    assert r.feed(w, 7050.2, text, late) == [("N5QLZ", 7050.2)]      # 1 + 2 copies
    later = late + REPEAT_S + 61.0
    assert r.feed(w, 7050.2, text, later) == []                       # the same window id counts afresh
    assert r.feed(r.window_id(1, 20, later), 7050.2, "CQ WES DE N5QLZ N5QLZ", later) == [("N5QLZ", 7050.2)]


def test_call_not_in_scp_spots_on_repeats() -> None:
    r = _rule()
    assert r.feed(r.window_id(1, 0, 0.0), 14030.0, "CQ K9ZZZ", 0.0) == []
    assert r.feed(r.window_id(1, 1, 60.0), 14030.0, "CQ K9ZZZ", 60.0) == [("K9ZZZ", 14030.0)]


def test_call_not_in_scp_needs_keyword_and_its_tier() -> None:
    r = RepeatSpotRule(lambda _: 3)
    for k in range(4):
        assert r.feed(r.window_id(1, k, k * 60.0), 14030.0, "DE K9ZZZ K9ZZZ", k * 60.0) == []
    assert r.feed(r.window_id(1, 4, 240.0), 14030.0, "CQ K9ZZZ", 240.0) == []
    assert r.feed(r.window_id(1, 5, 300.0), 14030.0, "CQ K9ZZZ", 300.0) == []
    assert r.feed(r.window_id(1, 6, 360.0), 14030.0, "CQ K9ZZZ", 360.0) == [("K9ZZZ", 14030.0)]


def test_one_spot_per_hold_unless_moved() -> None:
    r = _rule()
    r.feed(r.window_id(1, 0, 0.0), 14030.0, "CQ K0TQ", 0.0)
    assert r.feed(r.window_id(1, 1, 60.0), 14030.0, "CQ K0TQ", 60.0) == [("K0TQ", 14030.0)]
    assert r.feed(r.window_id(1, 2, 120.0), 14030.0, "CQ K0TQ", 120.0) == []
    assert r.feed(r.window_id(2, 0, 180.0), 14035.0, "CQ K0TQ", 180.0) == []
    assert r.feed(r.window_id(2, 1, 240.0), 14035.0, "CQ K0TQ", 240.0) == [("K0TQ", 14035.0)]
    t = 240.0 + REPEAT_S
    assert r.feed(r.window_id(2, 2, t), 14035.0, "CQ K0TQ", t) == [("K0TQ", 14035.0)]


def test_spot_on_the_strongest_nearby_bin() -> None:
    """N3AD, 40m CWT 2026-10-08: a bin 350 Hz below him decoded his CQs too, weaker."""
    r = _rule()
    r.feed(r.window_id(1, 0, 0.0), 7037.9, "CQ TEST N3AD", 0.0, snr=15.0)
    assert r.feed(r.window_id(2, 0, 30.0), 7038.25, "CQ TEST N3AD", 30.0, snr=40.0) == []   # same audio
    assert r.feed(r.window_id(1, 1, 60.0), 7037.9, "CQ TEST N3AD", 60.0, snr=15.0) == [
        ("N3AD", 7038.25)]


def test_windows_elsewhere_do_not_complete_a_spot() -> None:
    """NP4E ran on 7032.0; one garbled window 6 kHz up must not spot him there."""
    r = _rule()
    r.feed(r.window_id(1, 0, 0.0), 7032.0, "CQ CWT NP4E", 0.0)
    assert r.feed(r.window_id(2, 0, 30.0), 7037.9, "CQ TEST N3AD9U NP4E", 30.0) == []
    assert r.feed(r.window_id(1, 1, 60.0), 7032.0, "CQ CWT NP4E", 60.0) == [("NP4E", 7032.0)]


def test_old_windows_are_forgotten() -> None:
    r = _rule()
    r.feed(r.window_id(1, 0, 0.0), 14030.0, "CQ K0TQ", 0.0)
    t = REPEAT_S + 120.0
    assert r.feed(r.window_id(1, 1, t), 14030.0, "CQ K0TQ", t) == []


def test_spot_rule_key_values() -> None:
    import pytest

    import sparkgap

    try:
        assert sparkgap._select_spot_rule("repeat") is True
        assert sparkgap._select_spot_rule("off") is False
        with pytest.raises(ValueError, match="spot_rule"):
            sparkgap._select_spot_rule("bogus")
    finally:
        sparkgap._select_spot_rule("off")


def test_itila2_defaults_to_the_repeat_rule() -> None:
    import sparkgap

    assert sparkgap._spot_rule_name({"cw_decoder": "itila2"}) == "repeat"
    assert sparkgap._spot_rule_name({"cw_decoder": "itila"}) == "off"
    assert sparkgap._spot_rule_name({}) == "repeat"  # itila2 is the default decoder
    # an explicit setting wins either way, for comparing the two rules on one decoder
    assert sparkgap._spot_rule_name({"cw_decoder": "itila2", "spot_rule": "off"}) == "off"
    assert sparkgap._spot_rule_name({"cw_decoder": "itila", "spot_rule": "repeat"}) == "repeat"


def test_tracker_spots_window_records() -> None:
    import sparkgap

    tracker = sparkgap.SpotTracker(set(SCP), set())
    # spot_rule is None-typed in sparkgap.py (no hints in Fred's code), so set it via __dict__
    tracker.__dict__["spot_rule"] = RepeatSpotRule(_tier2)

    def window(key: int) -> sparkgap.SpotIntent:
        return sparkgap.SpotIntent(call="", freq_khz=14030.0, snr_db=12.0, wpm=28, is_runner=True,
                                   window_id=key, bin_id=7, window_text="CQ CWT K0TQ")

    assert tracker.process_intent(window(1)) == []
    assert tracker.process_intent(window(1)) == []
    spots = tracker.process_intent(window(2))
    assert [(s["call"], s["freq_khz"], s["method"]) for s in spots] == [("K0TQ", 14030.0, "repeat")]


def test_tracker_drops_blacklisted_and_spots_fast_runners() -> None:
    import sparkgap

    tracker = sparkgap.SpotTracker(set(SCP), {"K0TQ"})
    # spot_rule is None-typed in sparkgap.py (no hints in Fred's code), so set it via __dict__
    tracker.__dict__["spot_rule"] = RepeatSpotRule(_tier2)

    def window(key: int, text: str, wpm: int = 28) -> sparkgap.SpotIntent:
        return sparkgap.SpotIntent(call="", freq_khz=14030.0, snr_db=12.0, wpm=wpm, is_runner=True,
                                   window_id=key, bin_id=7, window_text=text)

    assert tracker.process_intent(window(1, "CQ K0TQ")) == []
    assert tracker.process_intent(window(2, "CQ K0TQ")) == []
    # no speed limit on this path: WG3J reads 42-44 WPM on the 40m CWT 2026-10-08
    assert tracker.process_intent(window(3, "CQ W1AW", wpm=44)) == []
    assert [s["call"] for s in tracker.process_intent(window(4, "CQ W1AW", wpm=44))] == ["W1AW"]


def test_call_after_de_counts_when_cq_came_first_and_it_is_sent_twice() -> None:
    assert runner_calls("CQ SKCC CG EKCC DE K4DH K4DH") == ["K4DH"]
    assert runner_calls("NOT E 3KZE CQ I EE DE VE3N") == []
    assert runner_calls("CQ K4DH DE W1AW W1AW") == []             # K4DH is being called
    assert runner_calls("TU DE W1AW W1AW") == []
    assert runner_calls("CQ SKCC CG EKCC DEW1AW W1AW") == ["W1AW"]
    assert runner_calls("CQ K1ABC? FOO BAR BAZ DE W1XYZ W1XYZ") == []
    assert runner_calls("CQ K1ABC? FOO BAR BAZ DEW1XYZ W1XYZ") == []


def _feed_cq(r: RepeatSpotRule, calls: list[str], freq: float = 7032.4, t0: float = 0.0) -> list:
    """One window per call in order, each 'CQ CWT <call>'; returns all spots."""
    out = []
    for k, call in enumerate(calls):
        t = t0 + 60.0 * k
        out += r.feed(r.window_id(1, int(t), t), freq, f"CQ CWT {call}", t)
    return out


def test_repeated_bust_does_not_outrun_the_real_call() -> None:
    """W6AYC, 40m CWT 2026-10-08: the decoder copied W6AYK twice, W6AYC three times."""
    spots = _feed_cq(_rule(), ["W6AYC", "W6AYK", "W6AYC", "W6AYK", "W6AYC"])
    assert ("W6AYK", 7032.4) not in spots
    assert ("W6AYC", 7032.4) in spots


def test_bust_waits_while_the_real_call_leads() -> None:
    r = _rule()
    assert _feed_cq(r, ["K2TW", "K2TW", "K2W"])[0][0] == "K2TW"
    assert _feed_cq(r, ["K2W"], t0=180.0) == []


def test_glued_garbage_does_not_block_the_call() -> None:
    """K8BZ, 20m CWT 2026-09-23: one 'CQ CWT K8BZTRQ' among many clean copies."""
    assert _feed_cq(_rule(), ["K8BZ", "K8BZTRQ", "K8BZ"]) == [("K8BZ", 7032.4)]


def test_near_identical_real_calls_both_spot() -> None:
    """N4VI and N4ZZ, 40m CWT 2026-10-08: two runners 0.1 kHz apart, two edits apart."""
    r = _rule()
    spots = _feed_cq(r, ["N4ZZ", "N4ZZ", "N4ZZ"], freq=7037.0)
    spots += _feed_cq(r, ["N4VI", "N4VI"], freq=7037.1, t0=200.0)
    assert {c for c, _ in spots} == {"N4ZZ", "N4VI"}


def test_a_one_khz_move_spots_again() -> None:
    r = _rule()
    assert _feed_cq(r, ["K0TQ", "K0TQ"], freq=7030.0) == [("K0TQ", 7030.0)]
    assert _feed_cq(r, ["K0TQ", "K0TQ"], freq=7031.0, t0=120.0) == [("K0TQ", 7031.0)]


def test_a_tie_with_a_one_edit_call_waits_for_the_next_copy() -> None:
    """Accepted cost of not guessing: a real call level with a one-edit call waits."""
    r = _rule()
    _feed_cq(r, ["K1ABC", "K1ABC"])
    assert _feed_cq(r, ["K1ABD", "K1ABD"], t0=120.0) == []
    assert _feed_cq(r, ["K1ABD"], t0=240.0) == [("K1ABD", 7032.4)]


def test_file_mode_counts_repeats_in_audio_time() -> None:
    import sparkgap

    tracker = sparkgap.SpotTracker(set(SCP), set())
    tracker.__dict__["spot_rule"] = RepeatSpotRule(_tier2)
    audio = [0.0]
    tracker.clock = lambda: audio[0]

    def window(key: int) -> sparkgap.SpotIntent:
        return sparkgap.SpotIntent(call="", freq_khz=14030.0, snr_db=12.0, wpm=28, is_runner=True,
                                   window_id=key, bin_id=7, window_text="CQ CWT K0TQ")

    assert tracker.process_intent(window(1)) == []
    audio[0] = REPEAT_S + 60.0          # the second copy comes 11 audio minutes later
    assert tracker.process_intent(window(2)) == []


def test_portable_calls_stay_whole() -> None:
    """40m CWT 2026-10-08: WX7V/5 sent HK3/NP4Z and N5AW/0; split at / they spotted as pieces."""
    assert runner_calls("CQ CWT N5AW/0 N5AW/0") == ["N5AW/0"]
    assert runner_calls("TEST HK3/NP4Z TEST") == ["HK3/NP4Z"]
    assert runner_calls("CQ W1AW/5") == ["W1AW/5"]
    assert runner_calls("CQ CWT N5AW/ T") == ["N5AW"]
    assert runner_calls("CQ CWT EE/E") == []


# Lock-on (spot_rule_lockon). Texts are itila2 decodes of the MRCE DX cells (simulated CQ WW
# band, 2026-10-10): runners sign with TU <call> TEST, and the caller's zone is in cut numbers.

def _lockon() -> RepeatSpotRule:
    return RepeatSpotRule(_tier2, lockon=True)


def _feed(rule: RepeatSpotRule, texts: list[str], freq: float = 7019.3, t0: float = 0.0,
          step: float = 60.0) -> list[tuple[str, float]]:
    spots = []
    for k, text in enumerate(texts):
        spots += rule.feed(rule.window_id(1, k + int(t0), t0 + k * step), freq, text, t0 + k * step)
    return spots


def test_runner_groups_name_the_call_before_a_bare_test() -> None:
    assert runner_groups("TU OA4EFA TEST SN3A 5NN AT TU") == [("SN3A", "OA4EFA")]
    assert runner_groups("CQ OA4EFA TEST W1ZT 5NN AT TU") == [("OA4EFA", None)]   # TEST closes the CQ
    assert runner_groups("TU E 4X1MK TEST LY2A 5C2T") == [("LY2A", "4X1MK")]
    assert runner_groups("CQ TEST 4X1MK") == [("4X1MK", None)]


def test_without_lockon_the_caller_after_the_owner_signing_spots() -> None:
    texts = ["CQ OA4EFA TEST UA3KW 5NN 10", "TU OA4EFA TEST SN3A 5NN AT TU",
             "TU OA4EFA TEST SN3A 5NN AT TU"]
    assert ("SN3A", 7019.3) in _feed(_rule(), texts)


def test_lockon_caller_after_the_owner_signing_does_not_spot() -> None:
    texts = ["CQ OA4EFA TEST UA3KW 5NN 10", "TU OA4EFA TEST SN3A 5NN AT TU",
             "TU OA4EFA TEST SN3A 5NN AT TU"]
    spots = _feed(_lockon(), texts)
    assert [c for c, _ in spots] == ["OA4EFA"]        # the owner's TU OA4EFA TEST counts instead


def test_lockon_owner_signing_with_tu_counts() -> None:
    # one CQ, then only TU <call>: two copies within REPEAT_S spot at tier 2
    spots = _feed(_lockon(), ["CQ TEST N1DE", "TU N1DE K4FN 5NN 5 TU N1DE"])
    assert spots == [("N1DE", 7019.3)]
    assert _feed(_rule(), ["CQ TEST N1DE", "TU N1DE K4FN 5NN 5 TU N1DE"]) == []


def test_lockon_sign_off_with_stray_letters_does_not_count() -> None:
    # KF2A for KF2AT, 20m 2026-10-13: the call split at a gap and the sign-off completed the count
    r = RepeatSpotRule(_tier2, lockon=True)
    assert r.feed(r.window_id(1, 0, 0.0), 14025.0, "CQ CQ DE KF2A T K", 0.0) == []
    assert r.feed(r.window_id(1, 1, 60.0), 14025.0, "TU DE KF2A T K", 60.0) == []
    assert r.feed(r.window_id(1, 2, 120.0), 14025.0, "TU DE KF2A K", 120.0) == [("KF2A", 14025.0)]


def test_lockon_tu_caller_never_promotes_a_caller() -> None:
    # K4FN is only ever thanked; it never sends CQ, so it never owns the frequency
    rule = _lockon()
    assert _feed(rule, ["TU K4FN", "TU K4FN", "TU K4FN"], freq=7034.6) == []
    assert rule.owner(7034.6, 120.0) is None


def test_lockon_owner_in_an_exchange_does_not_count() -> None:
    rule = _lockon()
    _feed(rule, ["CQ TEST W6YH"])
    assert _feed(rule, ["W6YH 5NN 3"], t0=60.0) == []


def test_lockon_lapses_when_idle() -> None:
    rule = _lockon()
    _feed(rule, ["CQ TEST W6YH"])
    assert rule.owner(7019.3, LOCK_IDLE_S) == "W6YH"
    assert rule.owner(7019.3, LOCK_IDLE_S + 1) is None
    assert _feed(rule, ["TU W6YH"], t0=LOCK_IDLE_S + 1) == []


def test_lockon_passes_to_a_new_cq_and_only_near_its_frequency() -> None:
    rule = _lockon()
    _feed(rule, ["CQ TEST W6YH"])
    _feed(rule, ["CQ TEST EI4KF"], t0=60.0)
    assert rule.owner(7019.3, 61.0) == "W6YH"           # one copy does not take it
    _feed(rule, ["CQ TEST EI4KF"], t0=61.0)
    assert rule.owner(7019.3, 62.0) == "EI4KF"          # tier(call) copies do
    assert rule.owner(7020.3, 62.0) is None
    # a call just signed by the old owner is not suppressed once ownership moved
    assert runner_groups("TU W6YH TEST K1ABC") == [("K1ABC", "W6YH")]
    _feed(rule, ["TU W6YH TEST K1ABC", "TU W6YH TEST K1ABC"], t0=120.0)
    assert rule.owner(7019.3, 181.0) == "K1ABC"


def test_lockon_bare_repeat_of_the_owner_does_not_count() -> None:
    # a fragment named once after a garbled TEST must not confirm itself by repeating bare
    # (cdub89 on #27: W0P on a non-contest 20m hour); TU / DE / before a trigger word do count
    assert _feed(_lockon(), ["TEST W0P", "W0P W0P"]) == []
    assert _feed(_lockon(), ["TEST W0P", "DE W0P K"]) == [("W0P", 7019.3)]
    assert _feed(_lockon(), ["TEST W0P", "W0P CQ"]) == [("W0P", 7019.3)]


def test_lockon_owner_keeps_the_frequency_against_its_own_garbles() -> None:
    # 7046.5 kHz, AZ QSO Party 10-11 00Z: K6NR's CQ AZQP decoded twice as CQ A6QP K6NR
    rule = _lockon()
    spots = _feed(rule, ["CQ AZQP K6NR CNO", "TU K6NR CQ AZQP K6NR CNO",
                         "TU K6NR CQ A6QP K?NR", "CQ AZQP K6NR CNO", "TU K6NR CQ A6QP K6NR CNO"])
    assert [c for c, _ in spots] == ["K6NR"]
    assert rule.owner(7019.3, 300.0) == "K6NR"


def test_lockon_new_runner_takes_over_once_the_owner_is_gone() -> None:
    # W7QP takes the frequency after K6NR leaves; a call ending in QP is still a call
    rule = _lockon()
    _feed(rule, ["CQ AZQP K6NR CNO", "CQ AZQP K6NR CNO"])
    spots = _feed(rule, ["TU K6NR CQ W7QP W7QP", "CQ W7QP W7QP", "CQ W7QP W7QP"], t0=120.0)
    assert [c for c, _ in spots] == ["W7QP"]
    assert rule.owner(7019.3, 241.0) == "W7QP"


def test_lockon_garbled_owner_yields_to_the_call_it_garbles() -> None:
    # 14012.0 kHz, Lakehouse 10-11: BW3IO (a garble, tier 4) took the frequency and confirmed
    # itself with TU BW3IO; W3IO (tier 2) sat in windows that also held BW3IO and never spotted
    rule = RepeatSpotRule(lambda c: 4 if c == "BW3IO" else 2, lockon=True)
    spots = _feed(rule, ["CQ CQ DE BW3IO K", "TU BW3IO CQ DE W3IO W3IO K",
                         "CQ DE W3IO BW3IO K", "CQ CQ DE W3IO W3IO BW3IO K"])
    assert [c for c, _ in spots] == ["W3IO"]
    assert rule.owner(7019.3, 200.0) == "W3IO"


def test_lockon_garble_in_the_same_tier_does_not_take_over() -> None:
    # K3Q inside K3QIA, both tier 2: the owner keeps the frequency, as in the K6NR case
    rule = _lockon()
    spots = _feed(rule, ["CQ PQ K3QIA K", "CQ PQ K3QIA K CQ PQ K3Q A", "CQ PQ K3Q K3QIA K"])
    assert [c for c, _ in spots] == ["K3QIA"]


def test_lockon_fragment_of_the_owner_does_not_take_over() -> None:
    # PA QSO Party 10-10, 7036.5 kHz: W2L, the front of the mobile W2LCQ/NLI, is not one edit
    # from it, so it stays a garble even though W2LCQ/NLI is in a less trusted tier
    rule = RepeatSpotRule(lambda c: 4 if "/" in c else 2, lockon=True)
    spots = _feed(rule, ["CQ PAQP W2LCQ/NLI", "CQ PAQP W2LCQ/NLI", "CQ PAQP W2L RQ/NLI W2LCQ/NLI",
                         "CQ PAQP W2L W2LCQ/NLI", "CQ PAQP W2LCQ/NLI W2L"])
    assert "W2L" not in [c for c, _ in spots]


def test_lockon_with_cq_groups_counts_each_group() -> None:
    # both keys on: lock-on picks the calls, cq_groups counts each CQ group of the window
    text = "CQ CQ WES WES DE N5QLZ N5QLZ K CQ CQ WES WES DE N5QLZ"
    both = RepeatSpotRule(_tier2, cq_groups=True, lockon=True)
    assert _feed(_lockon(), [text]) == []
    assert _feed(both, [text]) == [("N5QLZ", 7019.3)]
    assert both.owner(7019.3, 0.0) == "N5QLZ"


def test_sparkgap_runs_lockon_cq_groups_and_qualifiers() -> None:
    import sparkgap
    tracker = sparkgap.SpotTracker(set(SCP), set())
    for cfg, split in (({}, False), ({"spot_rule_split_runs": True, "spot_rule_lockon": False}, True)):
        rule = sparkgap._make_spot_rule(cfg, tracker)
        assert (rule.lockon, rule.cq_groups, rule.cq_qualifiers, rule.split_runs) == (True, True, True, split)


def test_two_band_runner_is_not_respotted_at_each_switch() -> None:
    # PA QSO Party 10-10: AA3B ran 40m and 80m at once and was spotted 103 times in 3 hours
    rule = _rule()
    spots = []
    for k in range(8):                               # alternating bands, two CQs each, 30 s apart
        f = 7040.0 if (k // 2) % 2 == 0 else 3540.0
        t = 30.0 * k
        spots += rule.feed(rule.window_id(int(f), k, t), f, "CQ PA AA3B AA3B", t)
    assert sorted(spots) == [("AA3B", 3540.0), ("AA3B", 7040.0)]   # once per band


def test_validation_level_must_be_known() -> None:
    with pytest.raises(ValueError):
        RepeatSpotRule(_tier2, validation="strict")
    with pytest.raises(ValueError):
        RepeatSpotRule(_tier2, validation="normal", listed_by="dta")


def test_regap_is_one_keying_with_the_gaps_moved() -> None:
    assert _regap("C0TT", "NN0TT")          # -.-. is C, or N N
    assert _regap("N7HR", "N7HRK/M")        # the call split at a long letter gap
    assert not _regap("K1ABC", "K1ABC")
    assert not _regap("W1AW", "K3JT")


def _validated(level: str, scp: bool) -> RepeatSpotRule:
    return RepeatSpotRule(_tier2, cq_groups=True, lockon=True, cq_qualifiers=True, validation=level,
                          listed=(lambda c: c in SCP) if scp else None)


def test_validation_doubled_listed_call_spots_on_its_first_cq() -> None:
    assert _feed(_validated("normal", True), ["CQ CQ DE K1ABC K1ABC K"]) == [("K1ABC", 7019.3)]


def test_validation_without_scp_needs_more_evidence() -> None:
    assert _feed(_validated("normal", False), ["CQ CQ DE K1ABC K1ABC K"]) == []
    assert _feed(_validated("permissive", False), ["CQ CQ DE K1ABC K1ABC K"]) == [("K1ABC", 7019.3)]


def test_validation_aggressive_waits_for_more_copies() -> None:
    r = _validated("aggressive", True)
    assert _feed(r, ["CQ CQ DE K1ABC K1ABC K"]) == []
    assert _feed(r, ["CQ CQ DE K1ABC K1ABC K"] * 3, t0=60.0) == [("K1ABC", 7019.3)]


def test_validation_rbn_list_weighs_more_than_scp() -> None:
    r = RepeatSpotRule(_tier2, cq_groups=True, lockon=True, cq_qualifiers=True, validation="aggressive",
                       listed=lambda c: c in SCP, listed_by="rbn")
    assert _feed(r, ["CQ CQ DE K1ABC K1ABC K"]) == []
    assert _feed(r, ["CQ CQ DE K1ABC K1ABC K"], t0=60.0) == [("K1ABC", 7019.3)]


def test_validation_rival_reading_lowers_confidence() -> None:
    plain, rival = _validated("normal", True), _validated("normal", True)
    _feed(rival, ["CQ CQ DE K1ABD", "CQ CQ DE K1ABD"])
    assert _feed(plain, ["CQ CQ DE K1ABC K1ABC K"], t0=120.0) == [("K1ABC", 7019.3)]
    assert _feed(rival, ["CQ CQ DE K1ABC K1ABC K"], t0=120.0) == []


def test_validation_off_is_accepted() -> None:
    from sparkgap import _make_spot_rule
    assert _make_spot_rule({"spot_validation": "off"}, None).threshold is None


def test_doubled_call_counts_twice_only_when_asked() -> None:
    """doubled: a call sent twice in one CQ group is two copies (hotairfred/SparkGap#43)."""
    assert runner_calls("CQ CQ DE K8TLJ K8TLJ K", every=True) == ["K8TLJ"]
    assert runner_calls("CQ CQ DE K8TLJ K8TLJ K", every=True, doubled=True) == ["K8TLJ", "K8TLJ"]
    assert runner_calls("CQ TEST W6YH TEST", every=True, doubled=True) == ["W6YH"]
    # a caller being sent the exchange twice is still no runner
    assert runner_calls("TEST N2MF N2MF 5NN 05", every=True, doubled=True) == []
    plain = RepeatSpotRule(_tier2, cq_groups=True)
    dbl = RepeatSpotRule(_tier2, cq_groups=True, doubled=True)
    assert plain.feed(1, 7030.0, "CQ CQ DE K8TLJ K8TLJ K", 0.0) == []
    assert dbl.feed(1, 7030.0, "CQ CQ DE K8TLJ K8TLJ K", 0.0) == [("K8TLJ", 7030.0)]
    lock = RepeatSpotRule(_tier2, cq_groups=True, lockon=True, doubled=True)
    assert lock.feed(1, 7030.0, "CQ CQ DE K8TLJ K8TLJ K", 0.0) == [("K8TLJ", 7030.0)]


def test_doubled_is_off_under_validation() -> None:
    """Validation scores a doubled call itself (its "doubled" weight); counting each copy too would count it twice."""
    assert RepeatSpotRule(_tier2, doubled=True).doubled
    assert not RepeatSpotRule(_tier2, doubled=True, validation="normal").doubled


def test_doubled_has_no_effect_with_validation() -> None:
    """The validation score already weighs a call sent twice; doubled must not add a second
    copy on top (one CQ CQ DE K1ABC K1ABC K would otherwise spot on its own)."""
    text = "CQ CQ DE K1ABC K1ABC K"
    for kw in ({}, {"doubled": True}):
        r = RepeatSpotRule(_tier2, cq_groups=True, lockon=True, validation="normal", **kw)
        assert r.feed(1, 7030.0, text, 0.0) == []


def test_local_port_validation_and_shared_list(monkeypatch) -> None:
    import sparkgap
    from spot_rule import VALIDATION
    tracker = sparkgap.SpotTracker(set(SCP), set())
    cfg = {"spot_validation": "normal", "spot_validation_calls": "scp"}
    assert sparkgap._make_spot_rule(cfg, tracker).threshold == VALIDATION["normal"]
    # the local port follows the top level unless its block says otherwise
    assert sparkgap._make_spot_rule(cfg, tracker, local={}).threshold == VALIDATION["normal"]
    assert sparkgap._make_spot_rule(cfg, tracker, local={"validation": "off"}).threshold is None
    assert sparkgap._make_spot_rule(cfg, tracker, local={"validation": False}).threshold is None
    assert sparkgap._make_spot_rule({}, tracker, local={"validation": "permissive"}).threshold == VALIDATION["permissive"]
    assert sparkgap._make_spot_rule({"spot_validation": "off"}, tracker).threshold is None
    # one list per kind, built once and shared by both rules
    monkeypatch.setattr(tracker, "_validation_lists", {}, raising=False)
    a = sparkgap._validation_list(cfg, tracker, "scp")
    b = sparkgap._validation_list(cfg, tracker, "scp")
    assert a is b and a("K0TQ") and not a("ZZ9ZZZ")


def test_split_up_counts_tu_call_up_only_when_asked() -> None:
    """split_up: a DX station working split signs TU <call> UP and may never send CQ
    (CY0S, 40m, 2026-03-19; hotairfred/SparkGap#48). Off by default, as in CW Skimmer."""
    text = "W5JMW 5NN TU CY0S UP"
    assert runner_calls(text, every=True) == []
    assert runner_calls(text, every=True, split_up=True) == ["CY0S"]
    # only the call right before UP: the callers he works after it are not runners
    assert runner_calls("TU CY0S UP KD4JG KD4JG KE3JP 5NN TU CY0S UP", every=True,
                        split_up=True) == ["CY0S", "CY0S"]
    assert runner_calls("TU CY0S UP XE1CQ", every=True, split_up=True) == ["CY0S"]
    # a bare <call> UP or a TU without UP is no sign-off (#43: those let contest junk through)
    assert runner_calls("CY0S UP", every=True, split_up=True) == []
    assert runner_calls("TU CY0S 5NN", every=True, split_up=True) == []
    plain = RepeatSpotRule(_tier2, cq_groups=True, lockon=True)
    split = RepeatSpotRule(_tier2, cq_groups=True, lockon=True, split_up=True)
    for rule in (plain, split):
        for k in range(2):
            spots = rule.feed(k, 7024.0, "W5JMW 5NN TU CY0S UP", 60.0 * k)
    assert plain.owner(7024.0, 60.0) is None
    assert spots == [("CY0S", 7024.0)] and split.owner(7024.0, 60.0) == "CY0S"


def test_split_up_by_config_and_local_port() -> None:
    import sparkgap
    tracker = sparkgap.SpotTracker(set(SCP), set())
    assert not sparkgap._make_spot_rule({}, tracker).split_up
    assert sparkgap._make_spot_rule({"spot_rule_split_up": True}, tracker).split_up
    # the local port's rule has its own key; the RBN port's stays off
    cfg = {"local_spot_rule": {"split_up": True}}
    assert not sparkgap._make_spot_rule(cfg, tracker).split_up
    assert sparkgap._make_spot_rule(cfg, tracker, local=cfg["local_spot_rule"]).split_up
    assert not sparkgap._make_spot_rule(cfg, tracker, local={}).split_up


def test_contest_words_open_a_group() -> None:
    """contest_words: CWT, SST or MST right before a call of 4+ characters opens a CQ group (CWT 10-14
    19Z: K5AX sent CWT K5AX six times in a window and counted once). 3-character pieces do not."""
    text = "CWT K5AX CWT K5AX CQ CQDE K5AX CWT K5AX"
    assert runner_calls(text, every=True) == ["K5AX"]
    assert runner_calls(text, every=True, contest_words=True) == ["K5AX"] * 3   # the CQ closes a group
    assert runner_calls(text, every=True, contest_words=True, cq_repeats=True) == ["K5AX"] * 4
    assert runner_calls("CWT N0K E E", every=True, contest_words=True) == []
    assert runner_calls("MST W6KK MST W6KK", every=True, contest_words=True) == ["W6KK", "W6KK"]


def test_cq_repeats_count_back_to_back_cqs() -> None:
    """cq_repeats: CQ CWT N1DC CQ CWT N1DC is two copies; the second CQ used to close the first group.
    A different call after the closing CQ is still the next transmission (a caller), not a runner."""
    assert runner_calls("CQ FD N1DC CQ FD N1DC", every=True) == ["N1DC"]
    assert runner_calls("CQ FD N1DC CQ FD N1DC", every=True, cq_repeats=True) == ["N1DC", "N1DC"]
    assert runner_calls("CQ TEST W6YH TEST K1ABC", every=True, cq_repeats=True) == ["W6YH"]


def test_contest_words_and_cq_repeats_local_port() -> None:
    """Top-level keys set both rules; local_spot_rule's own keys override them (contest_words on the local
    port only, as skimmer1 runs it)."""
    import sparkgap
    tracker = sparkgap.SpotTracker(set(SCP), set())
    cfg = {"spot_rule_cq_repeats": True, "local_spot_rule": {"contest_words": True}}
    rbn = sparkgap._make_spot_rule(cfg, tracker)
    loc = sparkgap._make_spot_rule(cfg, tracker, local=cfg["local_spot_rule"])
    assert rbn.cq_repeats and not rbn.contest_words
    assert loc.cq_repeats and loc.contest_words


def test_fragments_piece_of_a_longer_call_waits() -> None:
    """fragments 'any': K1V does not spot while K1VUT is heard near the same frequency;
    'stray': TEST K1V U T gives no copy of K1V (K1VUT split at a letter gap)."""
    from spot_rule import _fragment, _stray_after, tokens
    assert _fragment("K1V", "K1VUT") and _fragment("N6T", "N6TQS")
    assert not _fragment("K1VU", "K1VUT")            # one shorter: the one-edit vote's case
    assert not _fragment("W1AW", "K1VUT")
    t = tokens("CQ TEST K1V U T")
    assert _stray_after(t, 2)
    assert not _stray_after(tokens("CQ TEST K4TZ E E"), 2)   # noise after a full call
    assert not _stray_after(tokens("CQ TEST W0UI W 0 UI"), 2)
    plain = RepeatSpotRule(_tier2, cq_groups=True, lockon=True)
    frag = RepeatSpotRule(_tier2, cq_groups=True, lockon=True, fragments="any,stray")
    texts = ["CQ TEST K1VUT K1VUT", "CQ TEST K1V UT", "CQ TEST K1V U T", "CQ TEST K1V UT"]
    got_plain, got_frag = [], []
    for k, tx in enumerate(texts):
        got_plain += plain.feed(k, 14024.0, tx, 60.0 * k)
        got_frag += frag.feed(k, 14024.0, tx, 60.0 * k)
    assert ("K1V", 14024.0) in got_plain
    assert ("K1V", 14024.0) not in got_frag
    import pytest
    with pytest.raises(ValueError):
        RepeatSpotRule(_tier2, fragments="even,any")


def test_fragments_by_config() -> None:
    import sparkgap
    tracker = sparkgap.SpotTracker(set(SCP), set())
    assert sparkgap._make_spot_rule({}, tracker).fragments is None
    r = sparkgap._make_spot_rule({"spot_rule_fragments": "any,stray"}, tracker)
    assert r.fragments == "any" and r.stray
