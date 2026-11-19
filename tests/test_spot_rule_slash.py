"""Portable calls: a base call read with its portable form goes out in that form (spot_rule._slash_form).
Texts are WF8Z-1 live decodes of 11-14 to 11-17, where RBN's CW Skimmer nodes spotted the slash form."""

from spot_rule import RepeatSpotRule, slash_forms, tokens


def _tier2(call: str) -> int:
    del call
    return 2


def _feed(rule: RepeatSpotRule, texts: list[str], freq: float = 7057.0, t0: float = 1000.0) -> list:
    spots = []
    for k, text in enumerate(texts):
        t = t0 + 30.0 * k
        spots += rule.feed(rule.window_id(1, k, t), freq, text, t)
    return spots


def test_slash_forms_whole_and_split_at_the_slash() -> None:
    assert slash_forms("CQ CQ DE VE2PID /W1 VE2PID /W1K E") == {"VE2PID/W1"}   # glued K dropped
    assert slash_forms("CQ CQ CQ DE W7KFI / 5K") == {"W7KFI/5"}
    assert slash_forms("CQ POTA WA2USA /P CQ POTA WA2USA/P") == {"WA2USA/P"}
    assert slash_forms("CQ CQ CQ TI5/ N3KS TI5X N3KS") == {"TI5/N3KS"}             # prefix form
    assert slash_forms("CQ DE W1AW/0 K") == {"W1AW/0"}


def test_slash_forms_skip_noise_and_prosigns() -> None:
    assert slash_forms("CQ CWT N5AW/ T") == set()        # T: a noise letter, not a suffix
    assert slash_forms("CQ DE K1ABC / K") == set()       # a prosign
    assert slash_forms("CQ DE K1ABC / BK") == set()
    assert slash_forms("CQ DE X1AB /EE") == set()
    assert slash_forms("CQ DE TIH / VA3RA") == set()     # no digit: not a prefix
    assert slash_forms("CQ CQ DE K1ABC K") == set()


def test_tokens_are_unchanged() -> None:
    """Copies are counted as before: DE KK8A KK8A /QRP is still two copies of KK8A."""
    assert tokens("DE KK8A KK8A /QRP") == ["DE", "KK8A", "KK8A", "QRP"]


def test_base_call_goes_out_in_its_portable_form() -> None:
    rule = RepeatSpotRule(_tier2)
    spots = _feed(rule, ["CQ CQ DE VE2PID /W1 VE2PID /W1 K", "CQ CQ DE VE2PID /W1 VE2PID /W1 K"])
    assert [c for c, _ in spots] == ["VE2PID/W1"]


def test_one_window_of_the_form_is_not_enough() -> None:
    """A suffix read once may be garbled (W7KFI/5E, SN1T/1T on the replays): the base call goes out."""
    rule = RepeatSpotRule(_tier2)
    spots = _feed(rule, ["CQ CQ DE VE2PID VE2PID K", "CQ CQ DE VE2PID /W1 VE2PID K"])
    assert [c for c, _ in spots] == ["VE2PID"]


def test_form_and_base_are_one_spot() -> None:
    """The base call sent as VE2PID/W1 is not spotted again as VE2PID/W1 from its own copies."""
    rule = RepeatSpotRule(_tier2)
    spots = _feed(rule, ["CQ DE VE2PID/W1 VE2PID/W1 K", "CQ DE VE2PID /W1 VE2PID /W1 K",
                         "CQ DE VE2PID/W1 VE2PID/W1 K", "CQ DE VE2PID /W1 VE2PID /W1 K"])
    assert [c for c, _ in spots] == ["VE2PID/W1"]


def test_call_without_a_form_is_unchanged() -> None:
    rule = RepeatSpotRule(_tier2)
    assert [c for c, _ in _feed(rule, ["CQ CQ DE K1ABC K1ABC K", "CQ CQ DE K1ABC K1ABC K"])] == ["K1ABC"]


def test_a_report_or_sign_off_after_a_stray_slash_is_no_area() -> None:
    from spot_rule import _SUFFIX_RE
    for area in ("W1", "VE3", "KH6", "W7", "7", "P", "QRP", "MM"):
        assert _SUFFIX_RE.match(area), area
    for word in ("73", "88", "55", "5NN", "599"):          # AE7AP / 73, K1ABC / 5NN (Chris on #190)
        assert not _SUFFIX_RE.match(word), word


def test_two_reads_in_one_window_are_enough() -> None:
    """CQ DE W1AW /0 CQ DE W1AW /0 (WF8Z-1 11-18 20:22Z): the form twice in the window the spot comes from.
    Counting windows sent W1AW while RBN's other skimmers spotted W1AW/0 (39 spots over 11-14..18)."""
    rule = RepeatSpotRule(_tier2)
    spots = _feed(rule, ["CQ CQ DE W1AW W1AW K", "CQ DE W1AW /0 CQ DE W1AW /0"])
    assert [c for c, _ in spots] == ["W1AW/0"]


def test_a_garbled_suffix_in_both_copies_of_a_window_is_one_read() -> None:
    """The two filter paths decode the same audio: W1AW /T in both copies is one read, not two."""
    rule = RepeatSpotRule(_tier2)
    spots = []
    for k in range(2):
        t = 1000.0 + 30.0 * k
        for text in ("CQ DE W1AW /T CQ DE W1AW", "CQ DE W1AW /T CQ DE W1AW"):
            spots += rule.feed(rule.window_id(1, k, t), 7057.0, text if k == 0 else "CQ DE W1AW W1AW K", t)
    assert [c for c, _ in spots] == ["W1AW"]
