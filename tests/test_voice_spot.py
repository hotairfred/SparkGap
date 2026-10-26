"""Runner-position cases for voice_spot.py (strict mode). Run: python3 -m pytest test_voice_spot.py"""
import os
import sys

import pytest

sys.argv = ['voice_spot', 'x', 'x', '--strict']
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'voice'))
import voice_spot as v  # noqa: E402

CASES = [
    ("CQ contest, CQ contest, Whiskey Foxtrot 8 Zulu, contest.", ["WF8Z"]),
    ("Contest, Whiskey Foxtrot 8 Zulu, contest.", ["WF8Z"]),
    ("Whiskey Foxtrot 8 Zulu.", []),
    ("Thanks for the contest, Kilo Charlie 5 Sierra Charlie Kilo, you're 59.", []),
    ("K1ABC, you're 59 05, thanks. Contest, Whiskey Foxtrot 8 Zulu.", ["WF8Z"]),
    ("Did I copy Kilo Delta Niner Yankee Uniform Zulu?", []),
    ("QRZ, this is November Oscar 2 November.", ["NO2N"]),
    ("Thank you for hunting. This is Whiskey 2 Quebec Mike India and Parks US 0680, QRZ.", ["W2QMI"]),
    ("Kilo 1 Hotel Lima Bravo calling from Massachusetts.", []),
    ("CQDX CQDX Echo Alpha Three Bravo Ocean X-ray, Echo Alpha Three Bravo Ocean X-ray.", []),
    ("CQ the Pennsylvania QSO party, this is Alpha Alpha 3 Bravo, Alpha Alpha 3 Bravo.", ["AA3B", "AA3B"]),
    ("CQ DX, this is Whiskey Florida 8 Zanzibar.", ["WF8Z"]),
    ("The radio in Texas 5 is nice, CQ.", []),
]


@pytest.mark.parametrize("text,calls", CASES)
def test_runner_position(text: str, calls: list[str]) -> None:
    assert [c for _t, c, _x in v.runner_hits([[0, 5, text, -0.2]])] == calls


MORE = [
    # "Fox" is F inside a call since 10-22 (Fred): KE2CHF is the real call; "Drive" is just more words
    ("CQ CQ CQ, this is Kilo Echo 2, Charlie, Hotel, Fox, Drive, calling CQ.", ["KE2CHF"]),
    ("CQ CQ, this is Whiskey 1 Alpha Whiskey, Portable Zero in Missouri, QRZ.", ["W1AW/0"]),
    ("CQ, this is Whiskey 1 Alpha Whiskey stroke zero.", ["W1AW/0"]),
]


@pytest.mark.parametrize("text,calls", MORE)
def test_interrupted_and_suffix(text: str, calls: list[str]) -> None:
    assert [c for _t, c, _x in v.runner_hits([[0, 5, text, -0.2]])] == calls


QUALIFIED = [
    ("CQ POTA, CQ POTA, November, Oscar, 2, November.", ["NO2N"]),
    ("CQ DX, CQ DX, this is Echo Alpha 3 Bravo Oscar X-ray.", ["EA3BOX"]),
    ("CQ Bigfoot, CQ Bigfoot, this is Whiskey, Seven Oscar, QRZ.", ["W7O"]),
    ("CQ CQ CQ CQ from Whiskey Five Charlie Bravo Oscar.", ["W5CBO"]),
    ("CQ on 20 meters, Echo India Zero Charlie Lima calling CQ.", ["EI0CL"]),
    ("Goodbye, I'm Sugar, 5 1 Denmark, X-ray. QRZ.", ["S51DX"]),
    ("KQ4RLN, this is KJ7LVZ.", []),
    ("73 PD7V N1XXU.", []),
]


@pytest.mark.parametrize("text,calls", QUALIFIED)
def test_cq_qualifiers_and_self_id(text: str, calls: list[str]) -> None:
    assert [c for _t, c, _x in v.runner_hits([[0, 5, text, -0.2]])] == calls


CUT_OFF = [
    # "four" written "for" between phonetics (Fred 10-21), and the letters that follow cut off
    ("Calling CQ, this is Kilo Foxtrot for India Zulu Echo, KF4I, CE, calling CQ.", ["KF4IZE"]),
    ("Calling CQ, this is KF4I, CE, calling CQ.", []),
    ("CQ contest, this is Whiskey Foxtrot 8 Zulu, for the contest.", ["WF8Z"]),
    ("CQ, this is Whiskey Foxtrot 8 Zulu, and standing by.", ["WF8Z"]),
]


@pytest.mark.parametrize("text,calls", CUT_OFF)
def test_for_four_and_cut_off_letters(text: str, calls: list[str]) -> None:
    assert [c for _t, c, _x in v.runner_hits([[0, 5, text, -0.2]])] == calls


def test_a_call_inside_a_longer_one_on_the_same_carrier_is_cut_off() -> None:
    g = lambda base, khz, t: {'base': base, 'khz': khz, 'first': t, 'last': t + 60, 'n': 2}
    groups = [g('KC5SCK', 14227.9, 500), g('KC5CK', 14227.9, 700), g('KC5CK', 14250.0, 700), g('AA3B', 7169.6, 0)]
    assert v.contained('KC5CK', 'KC5SCK') and v.contained('KF4I', 'KF4IZE') and not v.contained('AA3B', 'AA3K')
    assert [v.cut_off(x, groups) for x in groups] == [False, True, False, False]
    once = dict(g('KC5SCKK', 14227.9, 600), n=1)            # one stuttered copy can't cut a real call off
    assert not v.cut_off(groups[0], groups + [once])
    twice = dict(g('KC5SCKK', 14227.9, 600), n=2)           # twice, but never issued: cuts nothing off
    assert not v.cut_off(groups[0], groups + [twice], fcc={'KC5SCK', 'KC5CK'})
    assert v.cut_off(groups[0], groups + [twice], fcc=None)


def test_long_calls_and_one_by_ones_need_three_copies() -> None:
    fcc = {'KF4IZE'}
    assert v.copies_needed('KF4IZE', fcc) == 2 and v.copies_needed('EA3BOX', fcc) == 2
    assert v.copies_needed('KC5SCKK', fcc) == 3 and v.copies_needed('OH2ABCD', fcc) == 3
    assert v.copies_needed('K4M', fcc) == 3 and v.copies_needed('K4M', None) == 2


CQWW = [   # a CQWW SSB QSO as Fred described it (10-22)
    ("Contest, Whiskey Foxtrot 8 Zulu.", ["WF8Z"]),
    ("Contest, Whiskey Fox 8 Zulu.", ["WF8Z"]),
    ("November 8 X-ray Sierra.", []),
    ("November 8 X-ray Sierra, 59 04.", []),
    ("59 03.", []),
    ("Thanks, Whiskey Foxtrot 8 Zulu, contest.", ["WF8Z"]),
    ("Thanks, Whiskey Fox 8 Zulu, contest.", ["WF8Z"]),
    ("Thanks, Whiskey Foxtrot 8 Zulu.", []),
    ("Thanks, Whiskey Foxtrot 8 Zulu, 59 03.", []),
    ("The fox was in the garden, CQ.", []),
]


@pytest.mark.parametrize("text,calls", CQWW)
def test_cqww_qso(text: str, calls: list[str]) -> None:
    assert [c for _t, c, _x in v.runner_hits([[0, 5, text, -0.2]])] == calls


DIGIT_PAIRS = [   # 10-24 CQ WW hour: Whisper writes a call's digit pair as one number (P40W said dozens of times)
    ("Thanks, Papa 40 Whiskey.", ["P40W"]),
    ("Sikki Contest, Papa Forty Whiskey.", ["P40W"]),
    ("Thanks, Papa 4-0, Lima", ["P40L"]),
    ("you are number 40 in Mercer county", []),
    ("five nine forty", []),
    ("Thanks buddy, QRZ, Whiskey Bravo 2, Zulu, Alpha Bravo, 599", ["WB2ZAB"]),
]


@pytest.mark.parametrize("text,calls", DIGIT_PAIRS)
def test_a_digit_pair_between_call_letters(text: str, calls: list[str]) -> None:
    assert [c for c, _a, _b in v.calls_in(v.tokens(text))] == calls


def test_variants() -> None:
    assert v.variant('D5D', 'ED5D') and v.variant('VE9CF', 'VE9CFF') and v.variant('PY2NY', 'PY2NYPY')
    assert v.variant('CB1B', '1B1B') and v.variant('NL7WA', '3L7WA') and v.variant('VE7ZO', 'VE70O')
    assert not v.variant('K1ABC', 'K1ABC') and not v.variant('W1AW', 'K3LR') and not v.variant('N1UR', 'N2UR3')


def test_the_better_reading_of_two_variants_wins(monkeypatch) -> None:
    monkeypatch.setattr(v, '_SCP', {'HI8R', 'S53M', 'CB1B', 'TF3SG'})
    def g(call, n, t=100.0, khz=14287.9):
        return {'base': call, 'call': call, 'khz': khz, 'first': t, 'last': t + 300, 'n': n}
    hi, i8 = g('HI8R', 4), g('I8R', 9)                    # known beats unknown, whatever the copies
    assert v.outvoted(i8, [hi, i8]) and not v.outvoted(hi, [hi, i8])
    s, ms = g('S53M', 9), g('MS53M', 3)                   # known beats a longer unknown
    assert v.outvoted(ms, [s, ms]) and not v.outvoted(s, [s, ms])
    to, o1 = g('TO1O', 3), g('O1O', 6)                    # neither known: the call with front letters wins
    assert v.outvoted(o1, [to, o1]) and not v.outvoted(to, [to, o1])
    cb, b1 = g('CB2B', 25), g('1B2B', 2)                  # neither known: copies decide
    assert v.outvoted(b1, [cb, b1]) and not v.outvoted(cb, [cb, b1])
    far = g('I8R', 9, t=100.0 + 3 * 1800)                 # the same carrier much later: left alone
    assert not v.outvoted(far, [hi, far])
    f3 = g('F3SG', 5, khz=14262.0)                        # a known variant heard twice there, any position
    assert v.outvoted(f3, [f3], heard_n={14262.0: {'TF3SG': 2}})
    assert not v.outvoted(f3, [f3], heard_n={14262.0: {'TF3SG': 1}})


def test_one_spot_per_call_within_3_khz() -> None:
    sent = [('K9CT', 3736.9, 100.0)]
    assert v.same_spot(sent, 'K9CT', 3739.8, 500.0)          # the same voice a few kHz up
    assert not v.same_spot(sent, 'K9CT', 3745.0, 500.0)      # a real move
    assert not v.same_spot(sent, 'W3LPL', 3737.0, 500.0)
    assert v.same_spot(sent, 'K9CT', 3737.9, 500.0, respot_s=600)
    assert not v.same_spot(sent, 'K9CT', 3737.9, 800.0, respot_s=600)   # live: re-spot after 10 min


def _g(base, khz, ts):
    return {'base': base, 'call': base, 'khz': khz, 'first': ts[0], 'last': ts[-1], 'n': len(ts), 'ts': list(ts)}


def test_a_caller_on_a_runner_s_carrier_is_not_spotted() -> None:
    runner = _g('W1RCR', 14228.2, [1500, 1550, 1600, 1645, 1664, 1715, 1727, 1741])
    caller = _g('KF0WYR', 14228.2, [1664, 1724])
    assert v.caller_on_owned(caller, [runner, caller])
    assert not v.caller_on_owned(runner, [runner, caller])
    other = _g('KF0WYR', 14233.0, [1664, 1724])                # another carrier: no owner
    assert not v.caller_on_owned(other, [runner, other])
    late = _g('N2IC', 14228.2, [2400, 2420, 2450])             # takes the frequency W1RCR left at 1741
    assert not v.caller_on_owned(late, [runner, late])
    clipped = _g('I1X', 7166.4, [100, 130, 160, 190, 220, 250])  # Whisper's clipped II1X is not another station
    full = _g('II1X', 7166.4, [200, 260])
    assert not v.caller_on_owned(full, [clipped, full])


def test_a_call_on_neither_list_needs_three_copies(monkeypatch) -> None:
    monkeypatch.setattr(v, '_SCP', {'CR6K', 'IP4X'})
    assert v.copies_needed('CR6K', set()) == 2
    assert v.copies_needed('XR6K', set()) == 3          # Whisper's misreading, heard before the real call
    assert v.copies_needed('W1AW', {'W1AW'}) == 2       # a US call in the FCC list
