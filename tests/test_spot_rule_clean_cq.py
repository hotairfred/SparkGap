"""spot_rule_clean_cq: a call that signs one CQ cleanly (CQ CQ CQ DE WA6BBQ) adds CLEAN to its validation
score, so one clean window can spot; callers and calls with more after them gain nothing."""
from sparkgap import _make_spot_rule
from spot_rule import CLEAN, RepeatSpotRule, clean_signed, tokens

LISTED = {"WA6BBQ", "W3FE", "K4NGA", "KD3CNH"}


def _clean(text: str) -> set[str]:
    return clean_signed(tokens(text), lambda c: c in LISTED)


def test_clean_shapes() -> None:
    assert _clean("CQ CQ CQ DE WA6BBQ") == {"WA6BBQ"}                  # nothing after the call
    assert _clean("CQ DE F6FAI AR K E E") == {"F6FAI"}                 # a sign-off
    assert _clean("TEST DE KD3CNH KD3CNH TEST") == {"KD3CNH"}          # the same call again
    assert _clean("CQ CQDX DE W3FE W3F") == {"W3FE"}                   # one letter off, only W3FE listed


def test_not_clean() -> None:
    assert _clean("W3JPH DE K2AN K") == set()                          # no CQ: a caller's form
    assert _clean("CQ CQ DE IK1NPP IKE?SE") == set()                   # something else after the call
    assert _clean("CQ DE N7PRQ 5P E") == set()
    assert _clean("CQ CQ DE K4NGR K4NGA K4NGA K") == set()             # K4NGR one off a listed call: either may be the bust
    assert clean_signed(tokens("CQ CQDX DE W3FE W3F")) == set()        # one letter off needs the call list


def _rule(clean: bool) -> RepeatSpotRule:
    return RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True, validation="normal",
                          listed=lambda c: c in LISTED, listed_by="rbn+skcc", clean_cq=clean)


def test_one_clean_window_spots_only_with_the_option() -> None:
    assert CLEAN > 0
    text = "CQ CQ CQ DE WA6BBQ"
    for on, want in ((False, []), (True, [("WA6BBQ", 7048.0)])):
        r = _rule(on)
        assert r.feed(r.window_id(1, 0, 0.0), 7048.0, text, 0.0) == want


def test_a_caller_gains_nothing() -> None:
    r = _rule(True)
    assert r.feed(r.window_id(1, 0, 0.0), 14028.0, "TEST LY2J WA6BBQ WA6BBQ PRANAS 1843", 0.0) == []


def test_config_key() -> None:
    cfg = {"spot_rule": "repeat", "spot_validation": "normal"}
    assert _make_spot_rule(cfg, None).clean_cq is False
    assert _make_spot_rule({**cfg, "spot_rule_clean_cq": True}, None).clean_cq is True


def test_an_unlisted_1x1_gains_nothing() -> None:
    # N4V read cleanly for N5V (5 lost its last dit): a 1x1 is one element from another, so CLEAN needs it listed
    from collections import Counter
    for call, gains in (("N4V", False), ("KD3CNH", True)):
        got = {}
        for on in (False, True):
            r = _rule(on)
            r.feed(r.window_id(1, 0, 0.0), 21041.9, f"CQ CQ DE {call} {call} K", 0.0)
            near = [c for c in r._copies if c.call == call]
            got[on] = r.score(call, near, Counter(c.call for c in near), False)
        assert (abs(got[True] - got[False] - CLEAN) < 1e-9) is gains
