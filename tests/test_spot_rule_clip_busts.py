"""spot_rule_clip_busts: a call that is a longer call cut off at an end or a start (K1AY of K1AYZ, H8A of
KH8A), or with its first character's leading elements lost (MX4U of 3X4U), does not spot while that
longer call was decoded nearby and RBN's skimmer counts back it; real look-alikes still spot."""

from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import rbn_known
import sparkgap
from spot_rule import CLIP_S, RepeatSpotRule, clip_of

COUNTS = Counter({"K1AYZ": 14, "K1AY": 6, "W3PYF": 65, "W3P": 4, "KH8A": 108, "H8A": 4, "3X4U": 31,
                  "MX4U": 4, "WX4U": 1, "K6AR": 86, "N6AR": 163, "F5IN": 168, "F5IND": 39,
                  "SD6M": 114, "SD6MCWT": 115, "R6AP": 75, "N6AP": 11, "W1AW": 174})
S = COUNTS.__getitem__


def test_clips() -> None:
    assert clip_of("K1AY", "K1AYZ", S)            # an end cut off, 14 >= 2 x 6
    assert clip_of("W3P", "W3PYF", S)
    assert clip_of("H8A", "KH8A", S)              # a start cut off
    assert clip_of("KD0R", "KD0RC", Counter({"KD0RC": 28}).__getitem__)   # the clip never on RBN
    assert clip_of("MX4U", "3X4U", S)             # 3 ...-- read as M --
    assert clip_of("WX4U", "3X4U", S)             # ...or as W .--


def test_real_look_alikes_spot() -> None:
    assert not clip_of("K6AR", "N6AR", S)         # K -.- is not N -. with elements lost: two stations
    assert not clip_of("N6AP", "R6AP", S)         # N6AP has 10+ skimmers itself
    assert not clip_of("F5IN", "F5IND", S)        # the longer one is the weaker
    assert not clip_of("SD6M", "SD6MCWT", S)      # not twice the skimmers
    assert not clip_of("W1AW", "W1AW/3", S)       # a portable form
    assert not clip_of("K1AY", "K1AYZ", Counter({"K1AYZ": 9}).__getitem__)   # under 10 skimmers
    assert not clip_of("MX4U", "3X4U", S, keep=lambda c: c == "MX4U")       # MASTER.SCP vouches for it


def _rule(clip: bool) -> RepeatSpotRule:
    return RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True, clip_strength=S if clip else None)


def _spots(rule: RepeatSpotRule, texts: list[tuple[float, str]], f: float = 7050.0) -> list[str]:
    out = []
    for t, text in texts:
        out += [c for c, _ in rule.feed(rule.window_id(int(f * 10), int(t // 30), t), f, text, t)]
    return out


SCENE = [(0.0, "TU K1AYZ 5NN OH"), (60.0, "CQ CQ CQ DE K1AY K1AY K"), (120.0, "CQ CQ DE K1AY K1AY K")]


def test_the_clip_waits_only_with_the_option() -> None:
    assert _spots(_rule(False), SCENE) == ["K1AY"]
    assert _spots(_rule(True), SCENE) == []


def test_far_or_old_longer_call_does_not_count() -> None:
    assert _spots(_rule(True), SCENE, f=7050.0) == []
    rule = _rule(True)
    _spots(rule, SCENE[:1], f=7050.6)             # the longer call 0.6 kHz away
    assert _spots(rule, SCENE[1:], f=7050.0) == ["K1AY"]
    late = [(t + CLIP_S + 60, x) for t, x in SCENE[1:]]
    rule = _rule(True)
    _spots(rule, SCENE[:1])
    assert _spots(rule, late) == ["K1AY"]         # decoded more than 10 minutes before


def test_skimmer_counts_read_the_cache(tmp_path: Path) -> None:
    for day, rows in (("20261110", "K1AYZ\t14\nK1AY\t2\n"), ("20261111", "K1AY\t6\nW3PYF\t65\n")):
        (tmp_path / f"{day}_WF8Z_v{rbn_known.CACHE_VERSION}.calls").write_text(rows)
    got = rbn_known.skimmer_counts(tmp_path, "WF8Z", 7, today=date(2026, 11, 12))
    assert got == Counter({"K1AYZ": 14, "K1AY": 6, "W3PYF": 65})
    assert rbn_known.skimmer_counts(tmp_path, "WF8Z", 1, today=date(2026, 11, 11)) == Counter({"K1AYZ": 14, "K1AY": 2})


def test_config(tmp_path: Path) -> None:
    trk = sparkgap.SpotTracker({"K1AY"}, set())
    assert sparkgap._clip_strength({}, "rbn", trk) is None
    assert sparkgap._clip_strength({"spot_rule_clip_busts": True}, "scp", trk) is None
    (tmp_path / f"{datetime.now(timezone.utc).strftime('%Y%m%d')}_WF8Z_v{rbn_known.CACHE_VERSION}.calls").write_text("X\t1\n")
    f = sparkgap._clip_strength({"spot_rule_clip_busts": True, "rbn_known_dir": str(tmp_path),
                                 "callsign": "WF8Z"}, "rbn", trk)
    assert f is not None and f("NOCALL") == 0
