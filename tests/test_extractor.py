"""Unit tests for callsign extraction.

Covers T1 (digit-first prefix fix, WX7V review 2026-09-21) plus baseline
coverage of the pure extraction helpers so future regex/extractor changes
have a guard.

Runs under pytest, or standalone with no pytest installed:
    python3 tests/test_extractor.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sparkgap as sg  # noqa: E402


# ---------------------------------------------------------------------------
# _is_base_call — the shape filter T1 changed
# ---------------------------------------------------------------------------

def test_digit_first_prefixes_now_extract():
    """T1: real digit-first prefixes were silently dropped by the old
    `^[A-Z]{1,2}` regex. They must now pass."""
    for c in ['9A1AA', '4X4DK', '5B4AIF', '3A2MW', '2E0ABC',
              '4O3A', '3V8SS', '9M2CNC', '3DA0DE']:
        assert sg._is_base_call(c), f'{c} should be a base call (T1)'


def test_letter_first_calls_unchanged():
    """Regression: nothing that passed before may stop passing. Includes
    letter-then-digit prefixes (H4/T5) that already worked via backtrack."""
    for c in ['W1AW', 'K3LR', 'VE3KIU', 'DL1XYZ', 'KH6XX', 'N5RZ',
              'HA7A', 'T5AB', 'H44RK', 'PJ5AA', 'M7Z']:
        assert sg._is_base_call(c), f'{c} regression: should still pass'


def test_noise_and_nonstructural_rejected():
    """Noise / control tokens must stay rejected — the fix must not open an
    all-digit or malformed door."""
    for c in ['5EEE', '90ABC', '991AA', 'ABCD', 'TEST', '599', '5NN',
              '12345', 'K', 'A1', '', 'TOOLONGCALL']:
        assert not sg._is_base_call(c), f'{c} should be rejected'


def test_5nn_runon_rejected():
    """The optional leading digit must not admit run-on contest reports
    (5NN5TU from WX7V's tests; 5NN5NN/5NN2N seen in live CQP raw decodes)."""
    for c in ['5NN5TU', '5NN5NN', '5NN2N', '5NN5E']:
        assert not sg._is_base_call(c), f'{c} is a 5NN run-on, not a call'
    # 5N (Nigeria) is 5N + digit — must still pass
    assert sg._is_base_call('5N7M')


def test_length_bounds():
    assert sg._is_base_call('W1AW')          # 4, ok
    assert sg._is_base_call('VE3KIU')        # 6, ok
    assert not sg._is_base_call('W1')        # too short / no suffix
    assert not sg._is_base_call('KH6XYZAB')  # 8, too long


# ---------------------------------------------------------------------------
# _itila_extract_cq_call — CQ-adjacent runner extraction
# ---------------------------------------------------------------------------

def test_extract_cq_runner_letter_first():
    assert sg._itila_extract_cq_call('CQ TEST W1AW W1AW', {'W1AW'}) == 'W1AW'


def test_extract_cq_runner_digit_first():
    """End-to-end T1: a digit-first runner adjacent to CQ now extracts."""
    assert sg._itila_extract_cq_call('CQ CQ DE 9A1A 9A1A', {'9A1A'}) == '9A1A'


def test_extract_cq_runner_digit_first_wx7v_cases():
    # cases from WX7V's fork tests
    assert sg._itila_extract_cq_call('CQ CQ 9A1AA 9A1AA K', {'9A1AA'}) == '9A1AA'
    assert sg._itila_extract_cq_call('CQ 4X4DK 4X4DK', {'4X4DK'}) == '4X4DK'


def test_extract_cq_stops_at_answering_caller():
    # CWT: the caller answers right after the runner's call; both SCP-valid.
    # Used to tie 2-2 (CQ and CWT both trigger) and pick the caller by recency.
    scp = {'LA2US', 'DL2YET', 'YE4IFB', 'N4USB'}
    assert sg._itila_extract_cq_call(
        'CQ CWT LA2US ? DL2YET DAN 28985 TU BILL 8593 TU LA2US', scp) == 'LA2US'
    assert sg._itila_extract_cq_call(
        'CQ CWT YE4IFB S ISHSHESSS N4USB SUE 21531 TU YE4IFB', scp) == 'YE4IFB'


def test_extract_cq_keeps_garbled_repeat():
    # a near-repeat (1-2 chars off) of the runner's call is still collected,
    # so a garbled first copy loses to the clean repeat
    assert sg._itila_extract_cq_call('CQ CQ E E A2JD K2JD K', {'K2JD'}) == 'K2JD'
    assert sg._itila_extract_cq_call('CQ CQ E E A2JD K2JD K') == 'K2JD'


def test_extract_cq_none_without_trigger():
    # no CQ/contest token -> no runner
    assert sg._itila_extract_cq_call('W1AW 5NN TU', {'W1AW'}) is None


# ---------------------------------------------------------------------------
# _itila_extract_all_calls — repetition-based context extraction
# ---------------------------------------------------------------------------

def test_extract_all_requires_repetition():
    # a call seen once is not returned (min_count=2 default)
    assert sg._itila_extract_all_calls('W1AW noise blah') == []


def test_extract_all_returns_repeated_incl_digit_first():
    calls = sg._itila_extract_all_calls('W1AW zzz W1AW yyy 9A1A qqq 9A1A')
    assert 'W1AW' in calls and '9A1A' in calls


# ---------------------------------------------------------------------------
# Fuzzy CQ trigger — CHARACTERIZATION (known over-acceptance, WX7V review).
# NOT a T1 fix; documents current behavior so a later precision fix has a
# baseline. REST/BEST/WEST/TEXT currently fuzzy-match TEST; tighten separately.
# ---------------------------------------------------------------------------

def test_fuzzy_cq_overaccepts_documented():
    # Current behavior: 'REST' is a 1-char sub from 'TEST' -> fires the trigger.
    # This is a documented precision gap, tracked for a separate fix. If a
    # future change tightens the trigger, flip this assertion and note it.
    got = sg._itila_extract_cq_call('REST W1AW W1AW', {'W1AW'})
    assert got == 'W1AW', 'characterization: REST currently triggers CQ extraction'


# ---------------------------------------------------------------------------
# Cases merged from WX7V's fork tests (cdub89/SparkGap tests/test_extractor.py)
# ---------------------------------------------------------------------------

def test_wx7v_base_call_cases():
    for c in ['W1AW', 'K9MA', 'M7Z', 'HB9AMO', '4U1ITU']:
        assert sg._is_base_call(c), f'{c} should be a base call'
    for c in ['HB9AMOHBM', '5NN', '5NN5TU', 'TU', '599']:
        assert not sg._is_base_call(c), f'{c} should be rejected'


def test_wx7v_extract_cq_cases():
    cases = [
        ('CQ CQ DE W1AW W1AW K', 'W1AW'),
        ('CQ TEST K9MA/P', 'K9MA/P'),
        ('CQ PJ2 AG3I', 'PJ2/AG3I'),
        ('CQ CQ E E A2JD K2JD K2JD K', 'K2JD'),
        ('FB JIM N3BB DE N5RZ', None),
    ]
    for text, want in cases:
        got = sg._itila_extract_cq_call(text)
        assert got == want, f'{text!r}: got {got!r}, want {want!r}'


def test_wx7v_extract_all_needs_repeats():
    assert sg._itila_extract_all_calls('W1AW DE K2JD W1AW TU') == ['W1AW']


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f'PASS  {fn.__name__}')
            passed += 1
        except AssertionError as e:
            print(f'FAIL  {fn.__name__}: {e}')
        except Exception as e:
            print(f'ERROR {fn.__name__}: {type(e).__name__}: {e}')
    print(f'\n{passed}/{len(fns)} passed')
    sys.exit(0 if passed == len(fns) else 1)
