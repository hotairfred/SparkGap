"""spot_rule_lead_dits: a call that is a longer call read with its leading dits lost (MX4U and WX4U of
3X4U) does not spot while that longer call was decoded nearby in at least twice the windows; real
calls, portable forms and a real call with stray dits in front of a garbled copy still spot."""

import re
from pathlib import Path

import sparkgap
from spot_rule import CLIP_S, RepeatSpotRule, lead_dits_of


def test_lead_dits_of() -> None:
    assert lead_dits_of("MX4U", "3X4U")           # ...-- read as --
    assert lead_dits_of("WX4U", "3X4U")           # ...or as .--
    assert lead_dits_of("M5IMO", "SM5IMO")        # S ... lost whole: three dits
    assert not lead_dits_of("W1WC", "EW1WC")      # one dit (E): too often a real prefix
    assert not lead_dits_of("DJ7PR", "OE/DJ7PR")  # a portable form
    assert not lead_dits_of("K6AR", "N6AR")       # not a tail
    assert not lead_dits_of("H8A", "KH8A")        # a lost dah-dit-dah, not dits
    assert not lead_dits_of("3X4U", "3X4U")
    assert not lead_dits_of("MX", "3X")           # too short to judge


RAW = re.compile(r"^(\d\d):(\d\d):(\d\d) INFO ITILA raw\s+([\d.]+) kHz cost=([\d.]+): '(.*)'$")
DATA = Path(__file__).parent / "data" / "lead_dits_3x4u_12m.txt"   # WF8Z-1 live, 11-16 15:50-17:05Z, 12 m


def _rule(on: bool = True) -> RepeatSpotRule:
    return RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True, window_s=30.0, lead_dits=on)


def _feed(rule: RepeatSpotRule, texts: list[tuple[float, str]], f: float) -> None:
    for t, text in texts:
        rule.feed(rule.window_id(round(f * 10), int(t // 30), t), f, text, t)


def _feed_live(rule: RepeatSpotRule, until: str) -> float:
    t = 0.0
    for line in DATA.read_text().splitlines():
        m = RAW.match(line)
        if m and m[1] + ":" + m[2] <= until:
            t = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
            rule.feed(rule.window_id(round(float(m[4]) * 10), int(t // 30), t), float(m[4]), m[6], t)
    return t


def test_live_3x4u_busts_are_held() -> None:
    # the 16:46 and 17:02Z MX4U and 16:20 and 16:36Z WX4U spots went out on 24897.0 while 3X4U ran there
    for when, call in (("16:20", "WX4U"), ("16:36", "WX4U"), ("16:46", "MX4U"), ("17:02", "MX4U")):
        rule = _rule()
        now = _feed_live(rule, when)
        assert rule._lead_dit_bust(call, 24897.0, now), (when, call)
        assert not rule._lead_dit_bust("3X4U", 24897.0, now)


def test_the_longer_call_must_lead_two_to_one() -> None:
    # W0YB calls CQ; a garbled copy reads SW0YB (S ... in front): as often as W0YB, so W0YB is no bust
    rule = _rule()
    _feed(rule, [(0.0, "CQWES DE W0YB SW0YB S"), (30.0, "CQ WES DE W0YB W0YB"), (60.0, "CQ DE SW0YB W0YB K")], 7050.0)
    assert not rule._lead_dit_bust("W0YB", 7050.0, 60.0)
    # ...while a call read whole in 4 windows against its lead-lost copy's 2 is
    rule = _rule()
    _feed(rule, [(0.0, "CQ 3X4U UP"), (30.0, "TU 3X4U UP"), (60.0, "CQ 3X4U UP"), (90.0, "CQ MX4U UP"),
                 (120.0, "CQ 3X4U UP"), (150.0, "TU MX4U UP")], 10107.0)
    assert rule._lead_dit_bust("MX4U", 10107.0, 150.0)


def test_far_or_old_longer_call_does_not_count() -> None:
    texts = [(0.0, "CQ 3X4U UP"), (30.0, "TU 3X4U UP"), (60.0, "CQ 3X4U UP")]
    rule = _rule()
    _feed(rule, texts, 10107.6)                   # 0.6 kHz away
    assert not rule._lead_dit_bust("MX4U", 10107.0, 60.0)
    rule = _rule()
    _feed(rule, texts, 10107.0)
    assert not rule._lead_dit_bust("MX4U", 10107.0, 60.0 + CLIP_S + 1)   # over 10 minutes ago


def test_off_records_nothing() -> None:
    rule = _rule(False)
    _feed(rule, [(0.0, "CQ 3X4U UP")], 10107.0)
    assert rule._clip_toks == {}


def test_config_key_reaches_the_rule_and_the_settings_view() -> None:
    eff, _, _ = sparkgap._gui_settings({"spot_rule_lead_dits": True}, 192000)
    assert eff["spot_rule_lead_dits"] is True
    eff, _, _ = sparkgap._gui_settings({}, 192000)
    assert eff["spot_rule_lead_dits"] is False
