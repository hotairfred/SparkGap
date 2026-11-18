"""Gate spot rule (spot_rule2.py): one pass and one fail case per requirement."""

from spot_rule import tokens
from spot_rule2 import GateSpotRule, keyword_calls, said

RARE = {"KX9QQ"}          # unflagged patt3ch pattern: 3 reads
NONE = {"TT5T"}           # no pattern: 5 reads


def _shape(c: str) -> str | None:
    return None if c in NONE else ("rare" if c in RARE else "active")


def _rule(**kw) -> GateSpotRule:
    return GateSpotRule(_shape, **kw)


def _feed(rule: GateSpotRule, text: str, t: float = 0.0, f: float = 14025.0, w: int | None = None,
          snr: float | None = None) -> list[str]:
    win = rule.window_id(round(f * 10), int(t) if w is None else w, t)
    rule.feed(win, f, text, t, snr)
    return [c for c, _ in rule.end_pass()]


def _why(rule: GateSpotRule, call: str) -> int:
    return [r for _, _, c, r, _ in rule.trace if c == call][-1]


def test_spot_on_cq() -> None:
    r = _rule()
    assert _feed(r, "CQ CQ DE K1ABC K1ABC K") == ["K1ABC"]


def test_2_banned() -> None:
    r = _rule(banned=["K1ABC"])
    assert _feed(r, "CQ K1ABC K1ABC") == [] and _why(r, "K1ABC") == 2


def test_3_reads_by_pattern() -> None:
    r = _rule()
    assert _feed(r, "CQ CWT N5RG", w=1) == [] and _why(r, "N5RG") == 3          # + pattern: 1 of 2
    assert _feed(r, "CQ CWT N5RG", t=30, w=2) == ["N5RG"]                     # same signal: 2 reads
    assert _feed(r, "CQ KX9QQ KX9QQ", f=7030.0) == [] and _why(r, "KX9QQ") == 3   # rare: 2 of 3
    assert _feed(r, "CQ TT5T TT5T TT5T TT5T", f=3530.0) == []          # none: 4 of 5
    assert _why(r, "TT5T") == 3


def test_3_unlisted_call_needs_one_more_read() -> None:
    r = _rule(listed=lambda c: c == "K7IUK")
    assert _feed(r, "CQ K7IUK K7IUK") == ["K7IUK"]                               # listed: 2 of 2
    assert _feed(r, "CQ K7IUM K7IUM", f=7030.0) == [] and _why(r, "K7IUM") == 3   # unlisted: 2/3
    assert _feed(r, "CQ K7IUM", t=30, f=7030.0) == ["K7IUM"]                     # 3 reads


def test_3_buffer_is_256_characters() -> None:
    r = _rule()
    _feed(r, "CQ N5RG", w=1)
    _feed(r, "E " * 140, t=10, w=2)                                             # pushes N5RG out
    assert _feed(r, "CQ N5RG", t=20, w=3) == [] and _why(r, "N5RG") == 3


def test_4_best_reading() -> None:
    r = _rule()
    assert _feed(r, "CQ N5RZ N5RZ N5RZ CQ N5RG N5RG") == ["N5RZ"]
    assert _why(r, "N5RG") == 4


def test_5_keyword() -> None:
    r = _rule()
    assert _feed(r, "K1ABC K1ABC 5NN") == [] and _why(r, "K1ABC") == 5


def test_6_call_with_keyword() -> None:
    r = _rule()
    assert _feed(r, "TEST 5NN TU K1ABC K1ABC") == [] and _why(r, "K1ABC") == 7
    assert keyword_calls(tokens("TU K1ABC TEST")) == [(1, "TEST", False)]


def test_6_event_word_between_keyword_and_call() -> None:
    assert keyword_calls(tokens("CQ CQ POTA DE WA3N")) == [(4, "CQ", False)]
    assert keyword_calls(tokens("CQ CWT W4LOO")) == [(2, "CQ", False)]


def test_5_run_together_text() -> None:
    r = _rule()
    assert _feed(r, "CQCQDE K1ABC K1ABC") == ["K1ABC"]


def test_7_own_keyword() -> None:
    r = _rule()
    assert _feed(r, "K1ABC TEST DL1XYZ DL1XYZ DL1XYZ") == [] and _why(r, "DL1XYZ") == 8


def test_7_two_runners_on_one_frequency() -> None:
    r = _rule()
    assert _feed(r, "CQ K1ABC K1ABC", w=1) == ["K1ABC"]
    assert _feed(r, "CQ DL1XYZ DL1XYZ", t=20, w=2) == [] and _why(r, "DL1XYZ") == 4   # a tie
    assert _feed(r, "CQ DL1XYZ", t=40, w=3) == ["DL1XYZ"]


def test_9_stronger_copy_elsewhere() -> None:
    r = _rule()
    _feed(r, "CQ K1ABC K1ABC", t=0, f=14025.0, snr=25.0)
    assert _feed(r, "CQ K1ABC K1ABC", t=10, f=14073.0, snr=5.0) == [] and _why(r, "K1ABC") == 9


def test_10_respot() -> None:
    r = _rule()
    assert _feed(r, "CQ K1ABC K1ABC", t=0, w=1) == ["K1ABC"]
    assert _feed(r, "CQ K1ABC K1ABC", t=300, w=2) == [] and _why(r, "K1ABC") == 10
    assert _feed(r, "CQ K1ABC K1ABC", t=700, w=3) == ["K1ABC"]


def test_11_rbn_window() -> None:
    r = _rule(segments=[(14000.0, 14070.0)])
    assert _feed(r, "CQ K1ABC K1ABC", f=14095.0) == [] and _why(r, "K1ABC") == 11


def test_11_beacon() -> None:
    r = _rule()
    assert _feed(r, "4U1UN 4U1UN", f=14100.0) == ["4U1UN"]                 # no keyword needed


def test_3_call_validation_level_steps_the_reads() -> None:
    for level, need in (("permissive", 1), ("normal", 2), ("aggressive", 3)):
        r = _rule(level=level)
        assert _feed(r, "CQ" + " K1ABC" * (need - 1)) == []
        assert _feed(r, "CQ" + " K1ABC" * need, t=1.0) == ["K1ABC"]
    r = _rule(level="aggressive", listed=lambda c: False)
    assert _feed(r, "CQ K1ABC K1ABC K1ABC") == [] and _why(r, "K1ABC") == 3
    assert r.trace[-1][4] == "3 of 4 reads (active pattern, aggressive, not listed)"


def test_3_unknown_level_is_refused() -> None:
    import pytest
    with pytest.raises(ValueError, match="call validation level"):
        _rule(level="off")


def test_11_known_beacon_spots_on_one_read() -> None:
    r = _rule()
    assert _feed(r, "KH6RS T", f=21150.0) == ["KH6RS"]                     # once a slot
    assert _feed(r, "4U1UN T E ZL6B", f=14100.0) == ["4U1UN", "ZL6B"]     # beacons are not rivals
    assert _feed(r, "K6RS/B", f=28250.0) == [] and _why(r, "K6RS/B") == 3  # /B keeps gate 3
    assert _feed(r, "KH6RS", f=7025.0) == []                               # off a beacon frequency


def test_11_garbled_beacon_spots_the_beacon() -> None:
    r = _rule()
    assert _feed(r, "UH6RS T", f=21150.0) == ["KH6RS"]                     # the beacon, no garble
    assert _feed(r, "L6B G", f=14100.0) == ["ZL6B"]                        # a missing letter
    assert _feed(r, "OA2B", f=14100.5) == []                               # OH2B or OA4B: neither
    assert _feed(r, "UH6RS T", f=7025.0) == []                             # off a beacon frequency
    r = _rule(listed=lambda c: c == "OH2BH")
    assert _feed(r, "OH2BH", f=14100.0) == [] and _why(r, "OH2BH") == 3    # known: no garble


def test_5_keywords_only() -> None:
    r = _rule()
    assert _feed(r, "SS N6RO N6RO") == [] and _why(r, "N6RO") == 5
    assert keyword_calls(tokens("NA K1ABC")) == []


def test_6_qualifier_is_the_events_cq() -> None:
    r = _rule(qualifiers=["SS"])
    assert _feed(r, "SS N6RO N6RO") == ["N6RO"]
    assert _feed(_rule(qualifiers=["SS"]), "CQ SS N6RO N6RO", f=7030.0) == ["N6RO"]


def test_7_called_station_before_de() -> None:
    assert keyword_calls(tokens("CQ S52Y DE N8EM")) == [(3, "CQ", False)]


def test_7_caller_after_de_needs_a_keyword() -> None:
    assert keyword_calls(tokens("K1ABC DE W2XYZ")) == []


def test_7_tu_only_before_test() -> None:
    assert keyword_calls(tokens("TU K1ABC UP")) == []


def test_s_buffer_is_continuous() -> None:
    r = _rule()
    assert _feed(r, "TEST DE", w=1) == []
    assert _feed(r, "K1ABC K1ABC", t=30, w=2) == ["K1ABC"]


def test_4_most_read_wins_over_a_glued_tail() -> None:
    r = _rule()
    assert _feed(r, "CQ K1TR CQ K1TR CQ K1TRE") == ["K1TR"]
    assert _why(r, "K1TRE") == 3


def test_4_tie_spots_neither() -> None:
    r = _rule()
    assert _feed(r, "CQ N5AL N5AL CQ W5AL W5AL") == []
    assert _why(r, "N5AL") == 4 and _why(r, "W5AL") == 4


def test_7_read_with_exchange_is_never_tied() -> None:
    assert keyword_calls(tokens("CQ POTA KV0I KV0I TONY 445")) == []
    assert keyword_calls(tokens("CWT N4YDU N4YDU KEITH 2182"), frozenset({"CWT", "CQ"})) == []


def test_7_lookahead_skips_words_that_are_not_calls() -> None:
    assert keyword_calls(tokens("CQ SES WW1USA")) == [(2, "CQ", False)]
    assert keyword_calls(tokens("CQ CQ E E HFT ON6FT")) == [(5, "CQ", False)]
    assert keyword_calls(tokens("CQ BK W1AW")) == []                     # a sign-off ends the CQ
    assert keyword_calls(tokens("CQ K W1AW")) == [(2, "CQ", False)]      # K is no sign-off
    assert keyword_calls(tokens("CQ BT W1AW")) == [(2, "CQ", False)]     # BT continues


def test_9_strongest_same_signal_within_1_khz() -> None:
    r = _rule()
    _feed(r, "CQ K1ABC K1ABC", t=0, f=14025.0, snr=25.0)
    assert _feed(r, "CQ K1ABC K1ABC", t=10, f=14025.8, snr=5.0, w=9) == []
    assert _why(r, "K1ABC") == 10                                # same signal: a re-spot


def test_1_shape_accepts_digit_two_letter_prefix() -> None:
    assert keyword_calls(tokens("CQ 3DA0XYZ")) == [(1, "CQ", False)]


def test_s_buffer_ends_with_its_bin() -> None:
    r = _rule()
    _feed(r, "CQ K1ABC", w=1)
    r.close_bin(round(14025.0 * 10))
    assert _feed(r, "CQ K1ABC", t=30, w=2) == [] and _why(r, "K1ABC") == 3


def test_5_up_is_no_keyword() -> None:
    assert keyword_calls(tokens("TU UP LA9GX 5NN A LA9GX LA9GX 5NN")) == []


def test_9_a_copy_on_another_band_does_not_block() -> None:
    r = _rule()                   # CQWW: multi-ops CQ on two bands at once (hotairfred#173)
    _feed(r, "CQ K3LR K3LR", t=0, f=14025.0, snr=20.0)
    assert _feed(r, "CQ K3LR K3LR", t=20, f=21025.0, snr=18.0) == ["K3LR"]


def test_9_both_bands_of_one_pass_spot() -> None:
    r = _rule()                   # WebRX 11-14 13:46Z, OK/OM DX: OM3GI on 15 and 10 m, confirmed
    for f, snr in ((21012.3, 20.0), (28013.8, 18.0)):
        r.feed(r.window_id(round(f * 10), 1, 0.0), f, "CQ TEST OM3GI OM3GI", 0.0, snr)
    assert sorted(r.end_pass()) == [("OM3GI", 21012.3), ("OM3GI", 28013.8)]


def test_9_images_fed_before_the_real_copy_in_one_pass() -> None:
    r = _rule()
    for f, snr in ((7036.0, 17.0), (7061.5, 10.0), (7052.0, 88.0)):
        r.feed(r.window_id(round(f * 10), 1, 0.0), f, "CQ CQ DE WX7V WX7V WX7V K", 0.0, snr)
    assert [(c, f) for c, f in r.end_pass()] == [("WX7V", 7052.0)]


def test_9_images_within_6_db_of_each_other_spot_none() -> None:
    r = _rule()                               # WebRX 11-14 00:21:45Z, LakeDog on 7061
    copies = ((21004.1, 12.3), (21004.3, 12.4), (21000.1, 15.1))   # the 15 m copies
    for f, snr in copies:
        r.feed(r.window_id(round(f * 10), 1, 0.0), f, "CQ CQ DE WX7V WX7V WX7V K", 0.0, snr)
    assert r.end_pass() == []


def test_9_ranks_by_the_reported_snr() -> None:
    r = _rule()                   # WebRX 11-14 05:26:20Z, LakeDog on 7060
    for f, snr, rep in ((28060.0, 26.2, 35.9), (28025.2, 26.5, 22.7)):   # the 10 m copies
        win = r.window_id(round(f * 10), 1, 0.0)
        r.feed(win, f, "CQ CQ DE WX7V WX7V WX7V K", 0.0, snr, rep)
    assert r.end_pass() == [("WX7V", 28060.0)]


def test_2_bad_calls_file(tmp_path) -> None:
    from spot_rule2 import read_bad_calls
    (tmp_path / "BadCalls.txt").write_text("wx7v\n\nWX7U\n")
    r = _rule(banned=read_bad_calls(tmp_path / "BadCalls.txt"))
    assert _feed(r, "CQ WX7U WX7U") == [] and _why(r, "WX7U") == 2


def test_2_not_in_scp() -> None:
    scp = {"K1ABC", "NP4Z", "FR/G3TXF"}
    r = _rule(scp=scp.__contains__)
    assert _feed(r, "CQ K1ABC K1ABC") == ["K1ABC"]
    assert _feed(r, "CQ K1ABD K1ABD", f=7030.0) == [] and _why(r, "K1ABD") == 2
    assert r.trace[-1][4] == "not in SCP"
    assert _feed(r, "CQ HK3/NP4Z HK3/NP4Z", f=21030.0) == ["HK3/NP4Z"]        # its base call
    assert _feed(r, "CQ FR/G3TXF FR/G3TXF", f=3530.0) == ["FR/G3TXF"]         # listed portable
    assert _feed(r, "VE8AT T", f=14100.0) == ["VE8AT"]                        # a beacon need not be
    assert _feed(_rule(), "CQ K1ABD K1ABD", f=7030.0) == ["K1ABD"]            # off: no check


def test_2_a_known_call_needs_no_scp_entry() -> None:
    r = _rule(scp={"K1ABC"}.__contains__, listed=lambda c: c in {"KF0KFH"})
    assert _feed(r, "CQ KF0KFH KF0KFH KF0KFH") == ["KF0KFH"]                  # new ham, on the RBN list
    assert _feed(r, "CQ E2AD E2AD E2AD", f=7030.0) == [] and _why(r, "E2AD") == 2
    assert r.trace[-1][4] == "not in SCP or the known calls"


def test_11_notched_freqs_file(tmp_path) -> None:
    from spot_rule2 import read_notched
    (tmp_path / "NotchedFreqs.txt").write_text("7074\n14079.5-14081\n14090 14092\nnote\n")
    notched = read_notched(tmp_path / "NotchedFreqs.txt")
    assert notched == [(7074.0, 7074.0), (14079.5, 14081.0), (14090.0, 14092.0)]
    r = _rule(notched=notched)
    assert _feed(r, "CQ K1ABC K1ABC", f=14080.0) == [] and _why(r, "K1ABC") == 11
    assert _feed(r, "CQ K1ABC K1ABC", f=14025.0) == ["K1ABC"]


def test_4_a_split_call_reads_as_the_longer_call() -> None:
    r = _rule()                   # CWT 10-08 7023.9: K1VUT read whole and split by a gap
    assert _feed(r, "CQ CWT K1VUT TEST K1V UT TU K1V UT CQ CWT K1VUT", f=7023.9) == ["K1VUT"]


def test_4_a_cut_off_copy_loses_to_the_whole_call() -> None:
    r = _rule()                   # CWT 10-08 7042.0
    assert _feed(r, "CQ CWT N5TJ CQ CWT N5T T CQ CWT N5TJ CWT N5T M N5TJ", f=7042.0) == ["N5TJ"]
    assert _why(r, "N5T") == 4


def test_4_noise_letters_glued_on_are_no_longer_call() -> None:
    assert _feed(_rule(), "CQ K1TR CQ K1TR CQ K1TRE") == ["K1TR"]


def test_4_a_sign_off_after_the_call_is_no_split() -> None:
    r = _rule()                   # net 11-01 14038.1
    assert _feed(r, "CQ POTA DE KE8UEX KE8UEX K CQ POTA DE KE8UEX KE8UEXK", f=14038.1) == ["KE8UEX"]


def test_4_a_portable_form_is_the_same_station() -> None:
    r = _rule()                   # WebRX 11-14 14034.1: W1AW, then W1AW/7 read once
    assert _feed(r, "CQ CQ DE W1AW W1AW W1AW/7 K", f=14034.1) == ["W1AW"]


def test_4_a_call_with_its_start_cut_off_loses() -> None:
    r = _rule()                   # WX7V-2 11-14 14028.0: C6X of NC6X
    assert _feed(r, "CQ NC6X NC6X K CQ C6X C6X C6X", f=14028.0) == ["NC6X"]
    assert _why(r, "C6X") == 4


def test_4_a_glued_noise_letter_in_front_makes_no_longer_call() -> None:
    r = _rule()                   # WebRX 11-14 14055.0: SW0YB read 5x beside W0YB
    assert _feed(r, "CQ DE W0YB W0YB SW0YB K CQ DE W0YB SW0YB K", f=14055.0) == ["W0YB"]


def test_4_a_portable_form_is_no_rival() -> None:
    r = _rule()                   # Codex audit: W1AW read as often as W1AW/7
    assert "W1AW" in _feed(r, "CQ DE W1AW W1AW W1AW/7 W1AW/7 K", f=14034.1)


def test_trace_and_last_spots_are_bounded() -> None:
    import spot_rule2
    r = _rule()
    assert r.trace.maxlen == spot_rule2.TRACE_KEPT
    assert _feed(r, "CQ K1ABC K1ABC", t=0, w=1) == ["K1ABC"]
    _feed(r, "CQ DL1XYZ DL1XYZ", t=700, f=7030.0, w=2)
    assert all(c != "K1ABC" for c, _ in r._last_spot)


def test_window_lines_only_with_itila2_dump_windows(monkeypatch, caplog) -> None:
    import sparkgap
    st = {'snr': 12.0, 'pending': [], 'bin_seq': 7}
    with caplog.at_level("INFO", logger=sparkgap.log.name):
        sparkgap._emit_window(st, 14030.0, "CQ K1ABC", 25, 1)
        monkeypatch.setattr(sparkgap, "_DUMP_WINDOWS", True)
        sparkgap._emit_window(st, 14030.0, "CQ K1ABC", 25, 2)
    assert [r.getMessage() for r in caplog.records if "ITILA window" in r.getMessage()] == \
        ["ITILA window 2 bin 7 14030.0 kHz snr 12.0 snr500 None"]
    assert len(st['pending']) == 2


def test_a_spot_keeps_its_reason_and_the_latest_decision_is_kept_per_call() -> None:
    """cdub89/SparkGap#51 R10: SparkGap Web shows why a call spotted, or the gate that stopped it."""
    r = _rule()
    assert _feed(r, "CQ K1ABC K1ABC", f=14025.0) == ["K1ABC"]
    assert r.spot_why[("K1ABC", 14025.0)] == "Spotted CQ"
    assert r.latest("K1ABC", 14025.4) == (0, "Spotted CQ, 2 reads")
    _feed(r, "CQ K1ABC K1ABC", t=300, f=14025.0)                # gate 10: spotted 5 minutes ago
    assert r.latest("K1ABC", 14025.0) == (10, "spotted in the last 10 min")
    assert r.latest("K1ABC", 14027.0) is None                   # 2 kHz away: another signal
    assert r.latest("W1AW", 14025.0) is None


def test_two_spots_of_one_call_in_a_pass_keep_their_own_reasons() -> None:
    r = _rule()
    r.feed(r.window_id(1, 1, 0.0), 7025.0, "CQ K1ABC K1ABC", 0.0)
    r.feed(r.window_id(2, 1, 0.0), 14025.0, "CQ CQ K1ABC K1ABC K1ABC", 0.0)
    assert sorted(r.end_pass()) == [("K1ABC", 7025.0), ("K1ABC", 14025.0)]
    assert r.spot_why[("K1ABC", 7025.0)] == "Spotted CQ"
    assert r.spot_why[("K1ABC", 14025.0)] == "Spotted CQ"


def test_said_names_the_word_that_passed_or_the_group_that_failed() -> None:
    """SparkGap Web's words (cdub89/SparkGap#51 R10); the trace keeps the gate and its detail."""
    r = _rule(qualifiers=["POTA"])
    _feed(r, "CQ K1ABC K1ABC")
    _feed(r, "POTA N6RO N6RO", f=7030.0)
    _feed(r, "KH6RS T", f=21150.0)
    assert [r.spot_why[k] for k in (("K1ABC", 14025.0), ("N6RO", 7030.0), ("KH6RS", 21150.0))] == [
        "Spotted CQ", "Spotted POTA", "Spotted beacon"]
    _feed(r, "CQ KX9QQ", f=7040.0)
    assert said(*r.latest("KX9QQ", 7040.0)) == "Not copied clearly enough"
    assert said(2, "banned call") == "On the bad calls list"
    assert said(2, "not in SCP") == said(2, "not in SCP or the known calls") == "Not in SCP"
    assert [said(g, "") for g in (1, 3, 4, 5, 7, 8, 9, 10, 11)] == [
        "Not a valid call"] + ["Not copied clearly enough"] * 2 + ["Not heard calling CQ"] * 3 + [
        "Already spotted"] * 2 + ["Outside the spotted range"]


def test_portable_form_split_at_the_slash_goes_out_whole() -> None:
    # VE2PID /W1: the decoder put a word gap at the slash; read so in two windows, it spots as VE2PID/W1
    r = _rule()
    assert _feed(r, "CQ CQ DE VE2PID /W1 K", t=0.0, w=0) == []                  # 1 read of 2
    assert _feed(r, "CQ CQ DE VE2PID /W1 K", t=30.0, w=1) == ["VE2PID/W1"]
    assert r.spot_why == {("VE2PID/W1", 14025.0): "Spotted CQ"}          # under the call that went out


def test_one_window_of_a_portable_form_is_not_enough() -> None:
    r = _rule()
    assert _feed(r, "CQ CQ DE VE2PID VE2PID /W1 VE2PID K", t=0.0, w=0) == ["VE2PID"]


def test_portable_form_and_base_call_are_one_spot() -> None:
    r = _rule()
    _feed(r, "CQ CQ DE VE2PID /W1 K", t=0.0, w=0)
    assert _feed(r, "CQ CQ DE VE2PID /W1 K", t=30.0, w=1) == ["VE2PID/W1"]
    assert _feed(r, "CQ CQ DE VE2PID /W1 K", t=60.0, w=2) == []                 # gate 10: spotted


def test_a_report_after_a_stray_slash_is_no_portable_form() -> None:
    r = _rule()
    _feed(r, "CQ DE K1ABC / 5NN K1ABC", t=0.0, w=0)
    assert _feed(r, "CQ DE K1ABC / 73 K1ABC K", t=30.0, w=1) == ["K1ABC"]


def test_a_lead_dit_bust_is_never_the_best_reading() -> None:
    # 3X4U read whole more often than MX4U (its 3 ...-- lost two dits): gate 4's best reading holds MX4U
    r = _rule()
    _feed(r, "CQ 3X4U 3X4U UP TU 3X4U UP", t=0.0, w=0)
    _feed(r, "TU I MX4U UP CQ 3X4U UP", t=30.0, w=1)
    _feed(r, "CQ MX4U UP CQ 3X4U 3X4U UP", t=60.0, w=2)
    assert "MX4U" not in [c for _, _, c, g, _ in r.trace if g == 0]


def test_12_an_ncdxf_beacon_skips_gate_4() -> None:
    """The fading dashes between NCDXF beacons decode as noise words; one read of one tied a beacon
    at gate 4 (VE8AT and I6BT on WX7V-1, 11-17). A listed beacon keeps gate 4."""
    r = _rule()
    assert _feed(r, "VE8AT I6BT", f=14100.0) == ["VE8AT"]
    r = _rule(beacons={"W5ABC": 7039.0})
    assert _feed(r, "W5ABC I6BT", f=7039.0) == [] and _why(r, "W5ABC") == 4
