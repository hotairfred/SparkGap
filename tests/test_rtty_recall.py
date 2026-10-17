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
    assert sparkgap._rtty_range({}, 7050) is None
    assert sparkgap._in_digital_window(14075.2) and sparkgap._in_digital_window(7076.9)
    assert not sparkgap._in_digital_window(14085.0) and not sparkgap._in_digital_window(7065.0)
    assert sparkgap.RTTY_MAX_CANDIDATES == 100
