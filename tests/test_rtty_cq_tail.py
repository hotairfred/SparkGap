"""rtty_spot_cq_tail: a call sent twice right before a closing CQ, TEST or QRZ, after one other word, counts as
a copy (JARL W2FM W2FM CQ): an RTTY runner whose opening CQ was lost. Lines from the 10-17 JARTS and 10-10 PA
recordings."""
from spot_rule import RepeatSpotRule, runner_calls


def _calls(text: str) -> list[str]:
    return runner_calls(text, every=True, cq_tail=True)


def test_a_runner_whose_opening_cq_was_lost_counts() -> None:
    assert runner_calls("V KQ JARL W2FM W2FM CQ OZHZ", every=True) == []                # off
    assert _calls("V KQ JARL W2FM W2FM CQ OZHZ") == ["W2FM"]
    assert _calls("XQ MAKRO KA6BIM KA6BIM CQ") == ["KA6BIM"]
    assert _calls("ZQ WW K1NY K1NY TEST") == ["K1NY"]


def test_a_cq_group_that_already_named_the_call_counts_once() -> None:
    assert _calls("CQ JARL W2FM W2FM CQ") == ["W2FM"]
    assert _calls("TEST K1NY K1NY CQ") == ["K1NY"]


def test_callers_and_exchanges_do_not_count() -> None:
    for text in ("N6IE N6IE N6IE",                      # a caller, no CQ after
                 "DE NN5T NN5T K",
                 "K6TU K6TU TU KA6BIM CQ",               # a caller, then the runner's sign-off
                 "QQ DE K1NY K1NY CQ",                   # DE before the call: not a word of a CQ
                 "599 W5XB W5XB CQ",                     # an exchange before the call
                 "N4TL N4TL N4TL CQ"):                   # sent three times
        assert _calls(text) == runner_calls(text, every=True), text


def test_a_lost_cq_runner_spots_on_its_tier_with_lockon() -> None:
    rule = RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True, window_s=30.0, cq_tail=True)
    spots = []
    for k in range(2):
        w = rule.window_id(70516, k, 60.0 * k)
        spots += rule.feed(w, 7051.6, "V KQ JARL W2FM W2FM CQ OZHZ", 60.0 * k)
    assert spots == [("W2FM", 7051.6)]
