"""rtty_contest: a contest's own words open a CQ group, and a call followed by a grid square is a caller
(Makrothen, measured on the 10-10 recording); a call sent twice with the space lost is split."""
import sparkgap
from spot_rule import RepeatSpotRule, runner_calls

MAK = frozenset(sparkgap.RTTY_CONTESTS['makrothen']['words'])
WAE = frozenset(sparkgap.RTTY_CONTESTS['wae']['words'])


def test_a_doubled_call_run_together_is_split() -> None:
    assert sparkgap._rtty_split_keywords("TEST DE IV3WMSIV3WMS") == "TEST DE IV3WMS IV3WMS"
    assert sparkgap._rtty_split_keywords("TEST W1AW W1AW") == "TEST W1AW W1AW"
    assert sparkgap._rtty_split_keywords("XQZXQZ ABCDABCD") == "XQZXQZ ABCDABCD"     # not calls


def test_contest_words_open_a_group_when_the_cq_is_lost() -> None:
    text = "XHQ MAKRO AE1P AE1P CQBMLQ"
    assert runner_calls(text) == []
    assert runner_calls(text, contest_words=True) == []                 # CW's words: CWT, SST, MST
    assert runner_calls(text, contest_words=True, words=MAK) == ["AE1P"]


def test_a_call_sent_a_grid_square_is_a_caller() -> None:
    text = "TEST AA4DD AA4DD FN31 FN31"            # a runner's TEST, then the caller with its grid
    assert runner_calls(text, every=True) == ["AA4DD"]
    assert runner_calls(text, every=True, grid=True) == []
    assert runner_calls("CQ MAK K8IA K8IA CQ", every=True, grid=True) == ["K8IA"]   # runners unchanged
    # RR73 has a grid square's shape (RR = a field, 73) but is a sign-off: the runner is not a caller
    assert runner_calls("CQ MAK N4TL N4TL TU N4TL RR73", every=True, grid=True) == ["N4TL"]
    assert runner_calls("TEST N4TL N4TL RR73", every=True, grid=True) == ["N4TL"]


def test_the_contest_setting() -> None:
    assert sparkgap._rtty_contest({}) is None
    assert sparkgap._rtty_contest({'rtty_contest': 'Makrothen'})['exchange'] == 'grid'
    assert sparkgap._rtty_contest({'rtty_contest': 'nosuch'}) is None


def test_a_makrothen_runner_spots_from_contest_word_groups() -> None:
    rule = RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True, window_s=30.0,
                          contest_words=True, words=MAK, grid=True)
    spots = []
    for k in range(2):
        w = rule.window_id(708, k, 60.0 * k)
        spots += rule.feed(w, 7080.0, sparkgap._rtty_split_keywords("XQ MAKRO AE1P AE1P QQ"), 60.0 * k)
    assert spots == [("AE1P", 7080.0)]


def test_wae_words_open_a_group_and_qtc_traffic_spots_nobody() -> None:
    assert runner_calls("XQ WAE DL1ABC DL1ABC") == []
    assert runner_calls("XQ WAE DL1ABC DL1ABC", contest_words=True, words=WAE) == ["DL1ABC"]
    assert runner_calls("WAEDC TEST DL1ABC DL1ABC K", contest_words=True, words=WAE) == ["DL1ABC"]
    # the serial exchange is a caller's without any contest setting
    assert runner_calls("TEST DL1ABC DL1ABC K5TR K5TR 599 012", every=True) == ["DL1ABC"]
    # QTC traffic: a header, then time, call and serial per QSO relayed
    for text in ("QTC 3/10 1310 DL1ABC 034 1312 F5XYZ 021 1315 K1AB 044", "QTC? QTC 1/5 QRV? DL1ABC"):
        assert runner_calls(text, every=True, contest_words=True, words=WAE) == []
    assert sparkgap._rtty_contest({'rtty_contest': 'WAE'})['words'] == ('WAE', 'WAEDC')


def test_a_caller_after_the_runners_tu_wae_is_held_by_lock_on() -> None:
    rule = RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True, window_s=30.0,
                          contest_words=True, words=WAE)
    spots = []
    texts = ["CQ WAE DL1ABC DL1ABC CQ"] * 2 + ["TU K1XYZ DL1ABC WAE K1XYZ K1XYZ"] * 3
    for k, text in enumerate(texts):
        spots += rule.feed(rule.window_id(708, k, 60.0 * k), 7080.0, text, 60.0 * k)
    assert spots == [("DL1ABC", 7080.0)]
