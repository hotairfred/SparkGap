"""spot_validation (cdub89/SparkGap#38 SR6): two bins on one transmission count once, and the score
reads the better copy, whichever bin's window arrived first."""

from collections import Counter

from spot_rule import RepeatSpotRule

NOISY = ["CQ CQ KEA CQ DE N0DEX N?N T"]   # N0DEX 20m 10-23: one path, one copy
CLEAN = ["CQCQ CQDEN0DEX N0DEX N0DEX CQ", "CQCQ CQDEN0DEX N0DETA N0DEX CQ"]   # both paths


def _tier2(call: str) -> int:
    del call
    return 2


def _score(*bins: list[str]) -> tuple[float, int]:
    """Score of N0DEX after one window from each bin, 7 s apart; and its copy count."""
    r = RepeatSpotRule(_tier2, cq_groups=True, split_runs=True, window_s=30.0, validation="normal")
    for w, texts in enumerate(bins):
        win = r.window_id(w, 0, 7.0 * w)
        for text in texts:
            r.feed(win, 14030.2 + 0.1 * w, text, 7.0 * w, snr=10.0)
    near = [c for c in r._copies if c.call == "N0DEX"]
    return r.score("N0DEX", near, Counter(c.call for c in near), False), len(near)


def test_the_better_copy_scores_whichever_came_first() -> None:
    assert _score(NOISY, CLEAN)[0] == _score(CLEAN, NOISY)[0] == _score(CLEAN)[0]


def test_one_transmission_is_still_one_copy() -> None:
    assert _score(NOISY, CLEAN)[1] == _score(CLEAN, NOISY)[1] == 1
