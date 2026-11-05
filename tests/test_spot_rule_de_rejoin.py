"""Split runs after a CQ word, and DE rejoin with split runs (cdub89/SparkGap#37 SR3)."""
import pytest

from spot_rule import RepeatSpotRule, split_runs


def _tier2(call: str) -> int:
    return 2


@pytest.mark.parametrize(("text", "want"), [
    ("CQ POTADEWD9DWX", "CQ POTA DE WD9DWX"),
    ("CQ POTADEKN6FVK", "CQ POTA DE KN6FVK"),
    ("CQ SKCCDEW7ABC", "CQ SKCC DE W7ABC"),
    ("CQ SOTADE W7ABC", "CQ SOTA DE W7ABC"),
    ("TEST NADEK1ABC", "TEST NA DE K1ABC"),
])
def test_split_after_cq_word(text: str, want: str) -> None:
    assert split_runs(text) == want


@pytest.mark.parametrize("text", [
    "CQ ANDREW", "CQ POTA", "QRL POTADEW3RA", "NAME ANDREW", "CQ POTADEW D9DWX",
])
def test_split_after_cq_word_keeps_others(text: str) -> None:
    assert split_runs(text) == text


@pytest.mark.parametrize(("text", "want"), [
    ("CQ POTADEW D9DWX", "CQ POTA DE WD9DWX"),
    ("CQPOTADEW D9DWX", "CQ POTA DE WD9DWX"),
    ("CQ SKCCDEW 7ABC", "CQ SKCC DE W7ABC"),
    ("CQ NADEKN 6FVK", "CQ NA DE KN6FVK"),
    ("CQ DEW 1AW", "CQ DE W1AW"),
])
def test_de_rejoin(text: str, want: str) -> None:
    assert split_runs(text, de_rejoin=True) == want


@pytest.mark.parametrize("text", [
    "CQ CQ DE W D9DWX",       # a real gap after DE: the letter is not glued to DE
    "QRL DEW D9DWX",          # no CQ or TEST before the DE
    "DEW D9DWX",
    "CQ POTADEWD9DWX K",      # already a call after DE
])
def test_de_rejoin_leaves_others(text: str) -> None:
    assert split_runs(text, de_rejoin=True) == split_runs(text)


def _spots(rule: RepeatSpotRule, texts: list[str]) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for i, text in enumerate(texts):
        now = 1_000.0 + 30.0 * i
        out += rule.feed(rule.window_id(140_625, int(now), now), 14_062.5, text, now)
    return out


def test_rule_reads_the_rejoined_call() -> None:
    texts = ["CQ POTADEW D9DWX CQ POTADEW D9DWX"] * 3
    rule = RepeatSpotRule(_tier2, cq_groups=True, split_runs=True)
    assert {c for c, _ in _spots(rule, texts)} == {"WD9DWX"}


def test_de_rejoin_rides_with_split_runs() -> None:
    assert RepeatSpotRule(_tier2, split_runs=True).de_rejoin is True
    assert RepeatSpotRule(_tier2).de_rejoin is False
