"""Exchange test: a report followed by a county code (state QSO parties) marks the call
as the station being worked, as a report and a number does."""
from spot_rule import is_exchange, runner_calls, split_runs, tokens


def _calls(text: str) -> list[str]:
    return runner_calls(split_runs(text, True), every=True, qualifiers=True, la1d=True, gluek=True)


def test_report_and_county_is_an_exchange() -> None:
    """K0PC, PA 10-10 22Z: W0ZQ in Clark county giving him a report."""
    toks = tokens("W0ZQ/CLRK CQ CQ SDP WMOZQ/CLRK K0PC K0PC 5NN CLRK")
    assert is_exchange(toks, toks.index("K0PC"), qualifiers=True)
    assert _calls("W0ZQ/CLRK CQ CQ SDP WMOZQ/CLRK K0PC K0PC 5NN CLRK") == []


def test_report_then_a_prosign_or_keyword_is_not() -> None:
    assert _calls("CQ CQ DE K1ABC K1ABC 5NN TU") == ["K1ABC"]
    assert _calls("CQ CQ DE K1ABC K1ABC 5NN K") == ["K1ABC"]


def test_report_and_number_still_an_exchange() -> None:
    assert _calls("CQ TEST K1ABC 5NN 14") == []


def test_two_letters_after_the_report_are_not_a_county() -> None:
    assert _calls("CQ OA4EFA TEST SN3A 5NN AT TU") == ["OA4EFA"]
