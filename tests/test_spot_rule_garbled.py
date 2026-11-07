"""spot_rule_garbled_runner and spot_rule_stray_known: a caller after a misread runner call, and a
4-character split of a call copied here, are no CQ copies."""

from spot_rule import RepeatSpotRule, runner_calls, split_runs


def _calls(text: str, garbled: bool = True) -> list[str]:
    return runner_calls(split_runs(text, True), every=True, qualifiers=True, contest_words=True,
                        stray=True, garbled=garbled)


def test_caller_after_a_garbled_runner_is_no_copy() -> None:
    """The runner's call misread into a non-call: the call after it is a caller (11-06 live)."""
    for text in ("LA BK R TU 73 EE CQ POTAQ0SN K5LG 5NN MT",      # K0SN's POTA run
                 "VNITF 5NN TU W1AWD E5 N9NM CQ W1AWNR5 N9NM",     # W1AW/5
                 "CQ IK?VUT T E AJ8S AJ8S K AJ8SDE IU3VUT VYGM",
                 "CQ CWT MG?X KJ5T KJ5T FRED A88A"):
        assert "K5LG" not in _calls(text) and "N9NM" not in _calls(text)
        assert not {"AJ8S", "KJ5T"} & set(_calls(text))
    assert _calls("CQ POTAQ0SN K5LG 5NN", garbled=False) == ["K5LG"]   # off: as before


def test_runner_after_its_own_misread_copy_is_kept() -> None:
    """A misread copy of the runner's own call, a broken qualifier before DE, run-together keywords
    and a garbled call right after DE leave the runner's copy (11-05/06 live: real calls)."""
    for text, call in (("CQ CWT K?9X KG9X K", "KG9X"),
                       ("MM DX K CQ DX YET2M YU2M IX K", "YU2M"),
                       ("CQ ?P?D E?4ERU KQ4EFU", "KQ4EFU"),
                       ("EV TEST EE?DB K7DB K7DB", "K7DB"),
                       ("CQ DHM3E URE SM3EVR K E", "SM3EVR"),
                       ("LEETE QRLF CMT CQ SI?POAS DE KC2IRV 5", "KC2IRV"),
                       ("E Q QRL? QHL? CQ CQ?QDE K7GUD H", "K7GUD"),
                       ("DE K?VA K3VA 6 E CQ D6 CQ ?E DE K?SK K3VA K", "K3VA")):
        assert call in _calls(text), text


def _rule(stray_known: bool) -> RepeatSpotRule:
    return RepeatSpotRule(lambda c: 2, cq_groups=True, cq_qualifiers=True, lockon=True,
                          fragments="any,stray", stray_known=stray_known)


def test_split_of_a_call_copied_here_is_no_copy() -> None:
    """DE K1PUG then DE K1PU G K twice: K1PU is K1PUG split at a letter gap, while K1PUG has a copy
    within 1 kHz; unset, K1PU gets a copy (11-06 01:09Z, 7054.7, spotted live)."""
    for on, want in ((True, 0), (False, 1)):
        r = _rule(on)
        r.feed(r.window_id(1, 0, 0.0), 7054.7, "QRL ? CQ CQ CQ CQ DE K1PUG K", 0.0)
        r.feed(r.window_id(1, 1, 60.0), 7054.7, "DE K1PU G CQ K Q CQ DE K1PU G K", 60.0)
        assert sum(c.call == "K1PU" for c in r._copies) == want


def test_four_character_call_alone_is_kept() -> None:
    """No copy of a longer call near here: a 4-character call followed by a letter is itself (N2EP Q)."""
    r = _rule(True)
    r.feed(r.window_id(1, 0, 0.0), 7008.0, "QRL CQ CQ CQ DX DE N2EP Q N2EP Q CQ CQ", 0.0)
    assert any(c.call == "N2EP" for c in r._copies)
