"""rtty_spot_doubled_cq: a runner's own CQ with the call sent twice (CQ MAKRO K1MK K1MK) counts as
two copies; an RTTY caller sends their call two or three times right after the runner's CQ, and
those stay one copy. Lines from the 10-10 PA and 10-17 JARTS recordings."""
from spot_rule import RepeatSpotRule, runner_calls


def _calls(text: str, **kw) -> list[str]:
    return runner_calls(text, every=True, doubled_cq=True, **kw)


def test_a_runner_cq_with_the_call_twice_counts_twice() -> None:
    assert _calls("CQ MAKRO K1MK K1MK CIAOWN KLMSJLBTLKLUTMCGQB") == ["K1MK", "K1MK"]
    assert _calls("CQ MAK AJ6V AJ6V CQ I MWN") == ["AJ6V", "AJ6V"]
    assert _calls("PBTSZM CQ MAKRO WB8SIG WB8SIG JZHVESAAJSAOS") == ["WB8SIG", "WB8SIG"]
    assert _calls("TEST W1AW W1AW") == ["W1AW", "W1AW"]
    assert runner_calls("CQ MAKRO K1MK K1MK", every=True) == ["K1MK"]          # off: one copy


def test_a_caller_after_the_runners_cq_stays_one_copy() -> None:
    for text in ("CQ T K5VIP K5VIP K5VIP FM19 FM19",          # sent three times
                 "BV CQ PIHJDO K6TU K6TU XQ",                 # junk, not a contest word, after CQ
                 "KS0AA CQ K6TU K6TU XQ",                     # the runner's closing CQ, then a caller
                 "WUVPK CQ AJ6V AJ6V NTEVAJ6V TOM",           # a garbled third copy right after
                 "L 1U CQ K4FTO K4FTO K4;!59 VK4FTO FN43"):
        assert _calls(text) == runner_calls(text, every=True), text
        assert len(set(_calls(text))) == len(_calls(text)), text


def test_doubled_cq_is_off_under_validation() -> None:
    assert RepeatSpotRule(lambda c: 2, doubled_cq=True).doubled_cq
    assert not RepeatSpotRule(lambda c: 2, doubled_cq=True, validation="normal").doubled_cq


def test_an_active_call_spots_on_one_doubled_cq_with_lockon() -> None:
    text = "HO PWBJEEVXVPX CQ MAKRO K1MK K1MK CIAOWN"
    for on, want in ((False, []), (True, [("K1MK", 14090.1)])):
        rule = RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True, window_s=30.0, doubled_cq=on)
        assert rule.feed(rule.window_id(140901, 0, 0.0), 14090.1, text, 0.0) == want
