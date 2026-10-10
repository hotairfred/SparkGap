"""Repeat-evidence spot rule (spot_rule.py). Text cases are decodes from the 2026-09-23 replays."""

from spot_rule import LOCK_IDLE_S, REPEAT_S, RepeatSpotRule, runner_calls, runner_groups

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


def test_caller_sent_the_exchange_does_not_count() -> None:
    assert runner_calls("TEST AA3B FRED 2455") == []
    assert runner_calls("TEST VE7KW TEST K3JT K3JT KEITS 2182") == ["VE7KW"]


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
    assert r.feed(r.window_id(2, 0, 30.0), 7038.25, "CQ TEST N3AD", 30.0, snr=40.0) == [
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


def test_tracker_drops_blacklisted_and_overspeed() -> None:
    import sparkgap

    tracker = sparkgap.SpotTracker(set(SCP), {"K0TQ"})
    # spot_rule is None-typed in sparkgap.py (no hints in Fred's code), so set it via __dict__
    tracker.__dict__["spot_rule"] = RepeatSpotRule(_tier2)

    def window(key: int, text: str, wpm: int = 28) -> sparkgap.SpotIntent:
        return sparkgap.SpotIntent(call="", freq_khz=14030.0, snr_db=12.0, wpm=wpm, is_runner=True,
                                   window_id=key, bin_id=7, window_text=text)

    assert tracker.process_intent(window(1, "CQ K0TQ")) == []
    assert tracker.process_intent(window(2, "CQ K0TQ")) == []
    assert tracker.process_intent(window(3, "CQ W1AW", wpm=55)) == []
    assert tracker.process_intent(window(4, "CQ W1AW", wpm=55)) == []


def test_call_after_de_counts_when_cq_came_first_and_it_is_sent_twice() -> None:
    assert runner_calls("CQ SKCC CG EKCC DE K4DH K4DH") == ["K4DH"]
    assert runner_calls("NOT E 3KZE CQ I EE DE VE3N") == []
    assert runner_calls("CQ K4DH DE W1AW W1AW") == ["K4DH"]
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
    assert rule.owner(7019.3, 61.0) == "EI4KF"
    assert rule.owner(7020.3, 61.0) is None
    # a call just signed by the old owner is not suppressed once ownership moved
    assert runner_groups("TU W6YH TEST K1ABC") == [("K1ABC", "W6YH")]
    _feed(rule, ["TU W6YH TEST K1ABC", "TU W6YH TEST K1ABC"], t0=120.0)
    assert rule.owner(7019.3, 181.0) == "K1ABC"
