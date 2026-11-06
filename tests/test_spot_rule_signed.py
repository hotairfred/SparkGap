"""Validation's signed weight: a window where the call signs its own CQ (CQ ... DE WB8NER WB8NER K)
counts in the score; a caller (W3JPH DE K2AN K2AN) never signs."""
from spot_rule import SIGNED, RepeatSpotRule, signed_calls, tokens


def test_signed_calls() -> None:
    assert signed_calls(tokens("CQ CQ CQ DE WB8NER WB8NER WB8NER K")) == {"WB8NER"}
    assert signed_calls(tokens("CQ TEST DE K1ABC K1ABC")) == {"K1ABC"}
    assert signed_calls(tokens("W3JPH DE K2AN K2AN")) == set()          # no CQ: a caller's form
    assert signed_calls(tokens("CQ CQ POTA DE K4KNA K")) == set()       # sent once
    assert signed_calls(tokens("CQ DE W1AW K1ABC K1ABC")) == set()      # another call first


def _rule() -> RepeatSpotRule:
    return RepeatSpotRule(lambda c: 3, validation="normal", listed=lambda c: False, listed_by="rbn+skcc")


def test_an_unlisted_signed_cq_heard_twice_spots() -> None:
    """WB8NER, PA 10-10 22Z: not on the RBN list, two clean signed windows."""
    assert SIGNED > 0
    r = _rule()
    spots = []
    for k, t in enumerate((0.0, 30.0)):
        spots += r.feed(r.window_id(1, k, t), 14046.8, "CQ CQ CQ DE WB8NER WB8NER WB8NER K", t)
    assert spots == [("WB8NER", 14046.8)]


def test_the_same_copies_without_signing_wait() -> None:
    r = _rule()
    spots = []
    for k, t in enumerate((0.0, 30.0)):
        spots += r.feed(r.window_id(1, k, t), 14046.8, "CQ CQ CQ WB8NER WB8NER K", t)
    assert spots == []


def test_one_signed_window_is_not_enough() -> None:
    r = _rule()
    assert r.feed(r.window_id(1, 0, 0.0), 14046.8, "CQ CQ CQ DE WB8NER WB8NER WB8NER K", 0.0) == []
