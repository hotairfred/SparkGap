"""RTTY recall (2026-10-17): wider ranges, a cap of 100, no candidates in the digital windows,
and CQ / TEST / DE cut out of junk tokens before the spot rule."""
import sparkgap
from spot_rule import RepeatSpotRule


def test_keywords_cut_out_of_junk() -> None:
    f = sparkgap._rtty_split_keywords
    assert f("EFXTEST AI4WW AI4WW") == "EFX TEST AI4WW AI4WW"
    assert f("CQPDE KQ4TNF KQ4TNF") == "CQ P DE KQ4TNF KQ4TNF"
    assert f("TOQTEST S53M S53M") == "TOQ TEST S53M S53M"
    # calls and plain words stay whole, even with CQ, TEST or DE inside
    assert f("CQ TEST W1DE W1DE DE KD4CQZ") == "CQ TEST W1DE W1DE DE KD4CQZ"
    assert f("DEAR TESTER") == "DE AR TEST ER"      # junk-shaped words are cut too; harmless


def test_glued_cq_now_spots_and_a_caller_still_does_not() -> None:
    rule = RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True)
    spots = []
    for k in range(2):
        spots += rule.feed(k, 7050.0, sparkgap._rtty_split_keywords("XQZTEST AI4WW AI4WW"), 60.0 * k)
    assert spots == [("AI4WW", 7050.0)]
    caller = RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True)
    out = []
    for k in range(3):
        out += caller.feed(k, 7060.0, sparkgap._rtty_split_keywords("TU W1AW CQPTEST N2XX 599 05"), 60.0 * k)
    assert ("N2XX", 7060.0) not in out


def test_ranges_and_digital_windows() -> None:
    assert sparkgap._rtty_range({}, 7090) == (-65000, 35000)
    assert sparkgap._rtty_range({"rtty_ranges_repeat": {"7090": [-50000, -10000]}}, 7090) == (-50000.0, -10000.0)
    assert sparkgap._rtty_range({}, 7050) == (-25000.0, 75000.0)   # any 40m centre: absolute kHz ranges
    assert sparkgap._in_digital_window(14075.2) and sparkgap._in_digital_window(7076.9)
    assert not sparkgap._in_digital_window(14085.0) and not sparkgap._in_digital_window(7065.0)
    # FT4 dials are RTTY territory in contests: no exclusion there
    assert not sparkgap._in_digital_window(14082.0) and not sparkgap._in_digital_window(7048.5)
    assert sparkgap.RTTY_MAX_CANDIDATES == 100


def test_keyword_split_keeps_a_call_whole() -> None:
    """A keyword inside a call stays (TESTW1ADE is TEST W1ADE, not TEST W1A DE); junk still splits."""
    from sparkgap import _rtty_split_keywords as split
    assert split("CQ TESTW1ADE") == "CQ TEST W1ADE"
    assert split("XQCQK1ADE CQ K1ADE") == "XQ CQ K1ADE CQ K1ADE"
    assert split("TESTK1ABCQ") == "TEST K1ABCQ"
    assert split("EFXTEST AI4WW") == "EFX TEST AI4WW"
    assert split("CQPDE KQ4TNF") == "CQ P DE KQ4TNF"


def test_keyword_split_spots_no_piece_of_the_call() -> None:
    """Six windows of XQCQK1ADE CQ K1ADE spot K1ADE, never K1A."""
    from sparkgap import _rtty_split_keywords as split
    from spot_rule import RepeatSpotRule
    r = RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True)
    got = []
    for k in range(6):
        got += r.feed(k, 14085.0, split("XQCQK1ADE CQ K1ADE"), 60.0 * k)
    calls = {c for c, f in got}
    assert "K1A" not in calls and "K1ADE" in calls


def test_rtty_range_any_centre() -> None:
    """Absolute kHz ranges: a band-name centre (7076.8 kHz at 192k since #63) still finds 40m RTTY."""
    from sparkgap import _rtty_range
    lo, hi = _rtty_range({}, 7090)
    assert (lo, hi) == (-65000.0, 35000.0)
    lo, hi = _rtty_range({}, 7076.8)
    assert abs(7076.8 + lo / 1000 - 7025.0) < 1e-6 and abs(7076.8 + hi / 1000 - 7125.0) < 1e-6
    assert _rtty_range({}, 10118) is None
    assert _rtty_range({"rtty_ranges_repeat": {"40m": [7030, 7100]}}, 7090) == (-60000.0, 10000.0)
    assert _rtty_range({"rtty_ranges_repeat": {"7090": [-5000, 5000]}}, 7090) == (-5000.0, 5000.0)
