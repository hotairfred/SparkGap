"""Pure timing fits in itila2_core.c: fit_unit and fit_letter_word_boundary.

Pinned to real run lists captured with ITILA2_DUMP_RUNS on 2026-09-22, so a
later change to the fits cannot silently undo them. Exercised through the
itila2_test_fit_unit / itila2_test_fit_letter_word ctypes hooks (200 Hz
envelope samples, same units as the dump)."""

import ctypes
import random
from pathlib import Path

import pytest

_LIB_PATH = Path(__file__).resolve().parent.parent / "libitila2.so"
if not _LIB_PATH.exists():
    pytest.skip("libitila2.so not built", allow_module_level=True)

_lib = ctypes.CDLL(str(_LIB_PATH))

_lib.itila2_test_fit_unit.argtypes = [
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_int,
    ctypes.c_double,
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_double),
]
_lib.itila2_test_fit_unit.restype = ctypes.c_double

_lib.itila2_test_fit_letter_word.argtypes = [
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_int,
    ctypes.c_double,
    ctypes.POINTER(ctypes.c_int),
]
_lib.itila2_test_fit_letter_word.restype = ctypes.c_double

_lib.itila2_test_estimate_wpm.argtypes = [
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_int,
]
_lib.itila2_test_estimate_wpm.restype = ctypes.c_double

_lib.itila2_test_pitch_wpm.argtypes = [
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_int,
    ctypes.c_double,
    ctypes.c_double,
]
_lib.itila2_test_pitch_wpm.restype = ctypes.c_double

# W6TED, 24 WPM, bin 14059.0, 20m run 2 (2026-09-22): the complete run list of the
# window that decoded 'W6TED ? DE W6 TE D', captured with ITILA2_DUMP_RUNS. Dits are
# 11 to 12 samples, dahs 33 to 34; the EM unit that day was 4.00.
W6TED_RUNS = (
    "-14 +12 -38 +11 -11 +11 -11 +34 -8 +12 -27 +33 -11 +11 -10 +12 -52 "
    "+33 -11 +29 -35 +10 -11 +32 -41 +28 -36 +32 -88 +8 -11 +10 -12 +6 -6 "
    "+21 -12 +9 -13 +28 -14 +11 -50 +8 -14 +9 -14 +8 -36 +5 -60 +9 -12 "
    "+11 -35 +9 -78 +16 -24 +2 -67 +10 -12 +5 -71 +7 -5 +8 -4 +5 -15 +9 "
    "-62 +13 -27 +6 -20 +4 -13 +10 -16 +9 -10 +8 -15 +5 -19 +4 -11 +7 "
    "-1726 +18 -26 +11 -11 +11 -13 +30 -29 +9 -31 +14 -4298 +4 -243 +2 "
    "-281 +3 -398 +3 -455 +3 -73 +3 -1448 +2 -17 +5 -1091"
)

# KE4D, 17 WPM, bin 14061.5, 20m run 2 (2026-09-22): the complete run list of the
# window that decoded 'KE4 D' (its known defect); captured with ITILA2_DUMP_RUNS.
# Dits 12 to 14 samples, dahs 38 to 42; the EM unit that day was 12.24.
KE4D_RUNS = (
    "-4 +12 -16 +11 -16 +12 -2577 +38 -14 +13 -15 +42 -14 +14 -14 +42 -14 "
    "+42 -14 +14 -14 +42 -59 +42 -14 +14 -15 +41 -14 +14 -14 +42 -14 +42 "
    "-14 +14 -14 +42 -72 +14 -14 +42 -14 +42 -14 +14 -32 +42 -14 +42 -14 "
    "+42 -37 +42 -23 +15 -14 +41 -66 +14 -14 +42 -14 +42 -42 +14 -14 +14 "
    "-14 +14 -14 +43 -13 +42 -45 +14 -14 +14 -14 +14 -41 +42 -13 +15 -13 "
    "+43 -32 +42 -14 +42 -14 +14 -1027 +38 -14 +14 -14 +42 -27 +14 -47 "
    "+14 -14 +14 -14 +14 -14 +13 -16 +41 -60 +42 -14 +14 -15 +13 -104 +41 "
    "-42 +14 -14 +13 -15 +42 -45 +40 -35 +12 -15 +13 -16 +41 -53 +7 -20 "
    "+12 -18 +14 -4 +8 -4 +4 -30 +1 -50 +8 -989 +2 -22 +7 -2764 +37 -32 "
    "+14 -14 +14 -14 +42 -37 +41 -20 +13 -15 +13 -15 +41 -33 +14 -15 +12 "
    "-16 +41 -15 +13 -32 +14 -14 +41 -15 +14 -14 +13 -76 +42 -14 +42 -14 "
    "+12 -16 +13 -20 +8 -65 +13 -14 +6 -2 +3 -23 +8 -20"
)

# W1AW, about 25 WPM, bin 7035.0, 40m night recording (2026-09-22): the complete
# run list of the window that decoded 'CQ C N T E NNQ CQ T EQ M1 M /EM K'. Dits
# are 8 to 14 samples, dahs 34 to 43, dahs outnumbering dits 34 to 17; the EM
# unit that night was 19.54.
W1AW_RUNS = (
    "-32 +34 -14 +40 -77 +14 -14 +38 -14 +40 -13 +42 -40 +9 -14 +41 -20 +10 "
    "-13 +37 -15 +42 -368 +35 -15 +38 -14 +37 -16 +39 -202 +37 -11 +9 -10 +43 "
    "-2348 +14 -12 +9 -36 +36 -13 +41 -12 +9 -13 +40 -65 +40 -11 +8 -117 +36 "
    "-15 +42 -11 +9 -14 +41 -226 +9 -11 +35 -14 +40 -28 +9 -12 +34 -17 +35 "
    "-15 +37 -17 +40 -27 +9 -14 +43 -17 +9 -14 +34 -110 +35 -9 +10 -10 +11 "
    "-10 +40 -11 +9 -140 +39 -15 +42 -10 +9 -185 +42 -6038"
)

# WA2TMF, about 20 WPM, bin 7043.0, same 40m night recording: the complete
# run list of the window that decoded 'DE WA2 TM A MT'. Dits 7 to 14 samples,
# dahs 30 to 38; the EM unit that night was 12.82.
WA2TMF_RUNS = (
    "-134 +7 -11 +31 -12 +10 -50 +32 -10 +11 -11 +10 -116 +32 -10 +11 -11 +10 "
    "-1697 +31 -12 +12 -11 +35 -12 +12 -36 +19 -244 +35 -12 +12 -12 +35 -12 "
    "+12 -46 +35 -12 +35 -51 +20 -262 +34 -83 +36 -98 +37 -10 +14 -10 +37 -10 "
    "+13 -41 +35 -186 +26 -11 +12 -12 +12 -44 +12 -89 +12 -11 +37 -11 +36 -52 "
    "+13 -11 +36 -81 +13 -10 +13 -11 +36 -12 +35 -12 +31 -100 +35 -76 +25 -12 "
    "+35 -376 +10 -11 +35 -305 +35 -11 +36 -71 +30 -232 +34 -11 +12 -242 +11 "
    "-10 +37 -10 +37 -39 +13 -10 +37 -57 +13 -10 +13 -11 +37 -10 +37 -10 +37 "
    "-70 +36 -54 +37 -11 +18 -2788 +9 -10 +13 -10 +38 -77 +13 -48 +13 -67 +12 "
    "-12 +12 -12 +10 -114 +36 -62 +12 -12 +12 -11 +36 -11 +36 -11 +36 -413 +8 "
    "-36 +12 -12 +36 -128 +35 -12 +12 -11 +12 -54 +11 -103 +12 -12 +35 -12 "
    "+35 -53 +12 -12 +35 -76 +11 -37 +35 -11 +36 -11 +30 -142"
)

# NN0D bin 7060.0, 40m night (2026-09-22): a weak fast window the decoder
# estimated at 60 WPM; marks are mostly 1 to 10 sample noise with a few
# real dahs of 54 to 71.
NOISY_7060_RUNS = (
    "+316 -9 +18 -3 +57 -10 +65 -7 +7 -3 +10 -17 +4 -41 +4 -2054 +4 -15 "
    "+319 -31 +6 -18 +11 -50 +7 -16 +3 -20 +30 -13 +22 -21 +3 -30 +4 -129 "
    "+8 -2390 +2 -10 +14 -4 +2 -9 +23 -6 +76 -16 +8 -7 +18 -182 +15 -218 "
    "+5 -14 +4 -14 +2 -76 +14 -3 +103 -625 +5 -1367 +163 -4 +66 -31 +15 "
    "-10 +17 -2 +44 -3 +173 -4 +19 -3 +2 -5 +7 -7 +4 -129 +10 -71 +3 -24 "
    "+41 -7 +34 -11 +39 -168 +1 -843 +4 -385 +3 -645 +221 -17 +8 -106 +9 -25"
)


def runs(spec: str) -> tuple[list[int], list[int]]:
    """Parse a dump-style run string into (is_mark, dur) lists. Tokens
    starting with + are marks, - are spaces; the sign is stripped."""
    is_mark: list[int] = []
    dur: list[int] = []
    for tok in spec.split():
        is_mark.append(1 if tok.startswith("+") else 0)
        dur.append(abs(int(tok)))
    return is_mark, dur


def call_fit_unit(
    spec: str, unit_in: float
) -> tuple[float, int, float]:
    """Call itila2_test_fit_unit on a parsed run spec."""
    is_mark, dur = runs(spec)
    n = len(is_mark)
    is_mark_arr = (ctypes.c_int * n)(*is_mark)
    dur_arr = (ctypes.c_int * n)(*dur)
    fitted = ctypes.c_int()
    dah_out = ctypes.c_double()
    unit = _lib.itila2_test_fit_unit(
        is_mark_arr, dur_arr, n, unit_in, ctypes.byref(fitted), ctypes.byref(dah_out)
    )
    return unit, fitted.value, dah_out.value


def call_fit_letter_word(spec: str, unit: float) -> tuple[float, int]:
    """Call itila2_test_fit_letter_word on a parsed run spec."""
    is_mark, dur = runs(spec)
    n = len(is_mark)
    is_mark_arr = (ctypes.c_int * n)(*is_mark)
    dur_arr = (ctypes.c_int * n)(*dur)
    fitted = ctypes.c_int()
    boundary = _lib.itila2_test_fit_letter_word(
        is_mark_arr, dur_arr, n, unit, ctypes.byref(fitted)
    )
    return boundary, fitted.value


def call_estimate_wpm(runs_str: str) -> float:
    """Call itila2_test_estimate_wpm on a parsed run spec."""
    is_mark, dur = runs(runs_str)
    n = len(is_mark)
    is_mark_arr = (ctypes.c_int * n)(*is_mark)
    dur_arr = (ctypes.c_int * n)(*dur)
    return _lib.itila2_test_estimate_wpm(is_mark_arr, dur_arr, n)


def test_fit_unit_w6ted() -> None:
    """Pinned behaviour: with the EM unit of 4.00 the mark-run fit returns 8.68
    for 11 to 12 sample dits (low, pulled by short noise marks) and a dah near
    27. A fix that lands nearer 11.5 / 33 should move these bounds up."""
    unit, fitted, dah_out = call_fit_unit(W6TED_RUNS, 4.0)
    assert fitted == 1
    assert 8.0 <= unit <= 12.5
    assert 24.0 <= dah_out <= 36.0


def test_fit_unit_ke4d() -> None:
    """With the EM unit of 12.24 the mark-run fit returns 12.65 for 12 to 14
    sample dits and a dah near 41, both within the operator's real timing."""
    unit, fitted, dah_out = call_fit_unit(KE4D_RUNS, 12.24)
    assert fitted == 1
    assert 11.7 <= unit <= 14.3
    assert 35.0 <= dah_out <= 47.0


def test_fit_unit_w1aw_dah_heavy() -> None:
    """Pinned defect: clean, strong keying where dahs outnumber dits 34 to
    17 still fails to fit. The median-relative noise filter discards the
    dits as outliers, so fitted stays 0. A fix must recover the unit near
    the operator's real 8 to 14 sample dits and 34 to 43 sample dahs."""
    unit, fitted, dah_out = call_fit_unit(W1AW_RUNS, 19.54)
    assert fitted == 1
    assert 8.5 <= unit <= 11.5
    assert 33.0 <= dah_out <= 45.0


def test_fit_unit_wa2tmf_dah_heavy() -> None:
    """Same pinned defect on a second dah-heavy window: dahs outnumber dits
    and the fit is rejected. A fix must recover the unit near the
    operator's real 7 to 14 sample dits and 30 to 38 sample dahs."""
    unit, fitted, dah_out = call_fit_unit(WA2TMF_RUNS, 12.82)
    assert fitted == 1
    assert 10.0 <= unit <= 14.0
    assert 29.0 <= dah_out <= 39.0


def test_fit_unit_too_few_marks() -> None:
    """Fewer than UNIT_FIT_MIN_MARKS interior marks (5 here): the passed
    unit is returned unchanged and nothing is fitted."""
    five_marks = "-100 +10 -100 +10 -100 +10 -100 +10 -100 +10 -100"
    unit, fitted, dah_out = call_fit_unit(five_marks, 7.82)
    assert unit == 7.82
    assert fitted == 0
    assert dah_out == 0.0


def test_fit_unit_too_few_survivors_stays_unfitted() -> None:
    """Eight interior marks of which three are 4-sample noise: the median
    filter leaves five survivors, below UNIT_FIT_MIN_MARKS, and the fit
    returns the passed unit unfitted. Pinned so the second pass added for
    dah-heavy windows never fits noise against a handful of real marks."""
    eight_marks = (
        "-100 +4 -100 +4 -100 +4 -100 +12 -100 +12 -100 +12 -100 +12 -100 +12 -100"
    )
    unit, fitted, dah_out = call_fit_unit(eight_marks, 8.0)
    assert unit == 8.0
    assert fitted == 0
    assert dah_out == 0.0


# Off-tone bin 7053.3, W1AW 20 WPM replay (2026-09-22): 20 runs of a window whose
# "marks" are all 1 or 2 samples, which pass 0 used to fit as dit 1 / dah 2.
W1AW_NOISE_RUNS = "-34 +2 -10 +2 -10 +2 -34 +2 -10 +2 -34 +2 -83 +2 -22 +2 -10 +1 -11 +2"


def test_fit_unit_noise_marks_unfitted() -> None:
    """Marks shorter than a 35 WPM dit are noise in both passes: no fit."""
    unit, fitted, dah_out = call_fit_unit(W1AW_NOISE_RUNS, 9.6)
    assert unit == 9.6
    assert fitted == 0
    assert dah_out == 0.0


def test_estimate_wpm_w6ted() -> None:
    """Real CW runs 15 to 25 WPM and never above 35, so marks shorter than
    the dit at 35 WPM (7 samples at 200 Hz) are noise. W6TED's 11 to 12
    sample dits are 20 to 22 WPM; today the estimator says 60 because it
    counts the short noise marks as dits."""
    wpm = call_estimate_wpm(W6TED_RUNS)
    assert 17.0 <= wpm <= 27.0


def test_estimate_wpm_ke4d() -> None:
    """Real CW runs 15 to 25 WPM and never above 35, so marks shorter than
    the dit at 35 WPM (7 samples at 200 Hz) are noise. KE4D's 12 to 14
    sample dits belong in that range."""
    wpm = call_estimate_wpm(KE4D_RUNS)
    assert 15.0 <= wpm <= 22.0


def test_estimate_wpm_noise_marks_no_estimate() -> None:
    """Real CW runs 15 to 25 WPM and never above 35, so marks shorter than
    the dit at 35 WPM (7 samples at 200 Hz) are noise. Eight 2 to 4 sample
    noise marks with no real keying give a failed estimate, not a WPM."""
    noise_marks = (
        "-100 +2 -30 +3 -40 +2 -25 +4 -35 +3 -30 +2 -45 +3 -30 +2 -100"
    )
    wpm = call_estimate_wpm(noise_marks)
    assert wpm == -1.0


def test_estimate_wpm_noisy_7060_not_pinned() -> None:
    """Real CW runs 15 to 25 WPM and never above 35, so marks shorter than
    the dit at 35 WPM (7 samples at 200 Hz) are noise. NN0D's weak, fast
    window is mostly 1 to 10 sample noise marks; an estimate at the clamp
    is a failed estimate, not a pinned 60 WPM."""
    wpm = call_estimate_wpm(NOISY_7060_RUNS)
    assert wpm == -1.0 or wpm <= 35.0


def test_letter_word_w6ted() -> None:
    """Pinned defect: at the fitted unit 8.68 the boundary lands near 44
    samples, under this operator's 46 to 52 sample letter gaps, which is why
    the window read 'W6 TE D'. A fix must place it between 53 and 64 samples
    (letters stay letters, the 65 sample word gaps stay words)."""
    boundary, fitted = call_fit_letter_word(W6TED_RUNS, 8.68)
    assert fitted == 1
    assert 40.0 <= boundary <= 48.0


def test_letter_word_ke4d_known_split() -> None:
    """Pinned defect: at the fitted unit 12.65 the boundary lands near 3.9
    units (49 samples), under KE4D's 49 sample letter gap, so 'KE4 D' splits.
    A fix must move it above 52 samples; this test then needs updating."""
    boundary, fitted = call_fit_letter_word(KE4D_RUNS, 12.65)
    assert fitted == 1
    assert 44.0 <= boundary <= 56.0


def test_letter_word_falls_back_without_word_gaps() -> None:
    """Only element gaps (about 1 unit) and letter gaps (about 3 units),
    no word-length gaps: the fit does not find three classes and the fixed
    5-unit rule is used."""
    only_element_and_letter = (
        "-10 +10 -11 +10 -30 +10 -29 +10 -10 +10 -31 +10 "
        "-11 +10 -30 +10 -10 +10 -10 +10 -29 +10 -31 +10"
    )
    boundary, fitted = call_fit_letter_word(only_element_and_letter, 10.0)
    assert fitted == 0
    assert boundary == 5.0 * 10.0


def test_letter_word_ignores_silence() -> None:
    """Gaps far longer than GAP_FIT_MAX_UNITS (idle time, not word gaps)
    are excluded from the fit, so adding two of them does not move the
    boundary."""
    boundary_before, _ = call_fit_letter_word(W6TED_RUNS, 10.0)
    with_silence = W6TED_RUNS + " -3000 +10 -3000"
    boundary_after, _ = call_fit_letter_word(with_silence, 10.0)
    assert abs(boundary_after - boundary_before) <= 1.0


# W1AW 5 and 10 WPM (Farnsworth, 15 WPM characters), bin 7052.6, 2026-09-22: the first
# 90 runs of a window. Letter gaps 297 to 298 / 107 to 109 samples, word gaps 697 to
# 699 / 256 to 257; the fixed 1/3/7 unit start and 20-unit idle cap read every letter
# gap as a word gap.
W1AW_05_RUNS = (
    "-363 +51 -13 +19 -13 +19 -13 +19 -13 +51 -697 +52 -14 +19 -298 +50 -14 +50 -14 +50 "
    "-298 +19 -14 +50 -14 +50 -699 +19 -13 +19 -13 +19 -13 +19 -13 +19 -699 +18 -14 +50 "
    "-14 +50 -298 +19 -14 +50 -14 +50 -14 +18 -298 +51 -13 +51 -697 +52 -14 +19 -13 +19 "
    "-13 +19 -13 +51 -699 +50 -297 +20 -298 +51 -13 +19 -13 +19 -13 +51 -298 +50 -699 "
    "+19 -13 +19 -298 +19 -13 +19 -13 +19 -697 +20"
)
W1AW_10_RUNS = (
    "-131 +50 -14 +18 -14 +18 -14 +18 -14 +50 -257 +51 -13 +19 -107 +52 -14 +50 -14 +50 "
    "-109 +18 -14 +50 -14 +50 -256 +20 -13 +51 -13 +51 -14 +50 -14 +50 -109 +50 -14 +50 "
    "-14 +50 -14 +50 -14 +51 -256 +19 -13 +51 -14 +50 -108 +19 -14 +50 -14 +50 -14 +18 "
    "-109 +51 -13 +51 -257 +50 -14 +18 -14 +18 -14 +18 -14 +50 -257 +51 -107 +20 -108 "
    "+51 -14 +18 -14 +18 -14 +50 -107 +52 -256 +20"
)


def test_letter_word_farnsworth_5wpm() -> None:
    boundary, fitted = call_fit_letter_word(W1AW_05_RUNS, 18.89)
    assert fitted == 1
    assert 298.0 < boundary < 697.0


def test_letter_word_farnsworth_10wpm() -> None:
    boundary, fitted = call_fit_letter_word(W1AW_10_RUNS, 18.64)
    assert fitted == 1
    assert 109.0 < boundary < 256.0


# N3QE, bin 7032.4, 40m CWT 2026-10-08 03:06Z: the first 60 runs of a window. Per-over
# measurement from the IQ gives a 33 ms dit (36 WPM, CW Skimmer 34); itila2 marks
# read 8 and 22 samples with 5 to 6 sample element gaps, so marks alone say 30 WPM.
N3QE_RUNS = (
    "-202 +23 -19 +8 -6 +8 -6 +22 -40 +22 -6 +8 -19 +9 -5 +9 -5 +9 -5 +22 -6 +22 "
    "-19 +22 -6 +22 -5 +9 -5 +23 -19 +8 -756 +23 -5 +8 -6 +22 -19 +9 -5 +22 -6 +22 "
    "-5 +22 -6 +22 -19 +23 -5 +8 -6 +8 -20 +8 -5 +23 -5 +22"
)


def call_pitch_wpm(spec: str, dit_dah: float, elem_letter: float) -> float:
    is_mark, dur = runs(spec)
    n = len(is_mark)
    return _lib.itila2_test_pitch_wpm(
        (ctypes.c_int * n)(*is_mark), (ctypes.c_int * n)(*dur), n, dit_dah, elem_letter
    )


def test_pitch_wpm_n3qe() -> None:
    assert 33.0 < call_pitch_wpm(N3QE_RUNS, 14.0, 12.0) < 37.0


def test_pitch_wpm_too_few() -> None:
    assert call_pitch_wpm("-100 +8 -6 +8 -6 +22 -100", 14.0, 12.0) == 0.0


_lib.itila2_test_gate_segments.argtypes = [
    ctypes.POINTER(ctypes.c_double),
    ctypes.POINTER(ctypes.c_int8),
    ctypes.c_int,
    ctypes.c_double,
    ctypes.c_double,
    ctypes.c_double,
    ctypes.c_double,
]
_lib.itila2_test_gate_segments.restype = ctypes.c_int

GATE_UNIT = 12  # 20 WPM at 200 Hz
GATE_SIGMA = 0.1


def _gate(env: list[float], marks: list[int]) -> list[int]:
    n = len(env)
    env_c = (ctypes.c_double * n)(*env)
    marks_c = (ctypes.c_int8 * n)(*marks)
    kept = _lib.itila2_test_gate_segments(env_c, marks_c, n, 1.0, 0.0, GATE_SIGMA**2, float(GATE_UNIT))
    assert kept >= 0
    return list(marks_c)


def test_gate_segments_keeps_keying_and_blanks_noise() -> None:
    rng = random.Random(1)
    # "PARIS" at 20 WPM: .--. .- .-. .. ...
    elements = "1011101110100000101110000010111010000010100000101010"
    keyed = [int(c) for c in elements for _ in range(GATE_UNIT)]
    pause = [0] * (12 * GATE_UNIT)
    # noise: the envelope stays at the noise level, but the marks claim dits
    blips = ([1] * GATE_UNIT + [0] * GATE_UNIT) * 6
    marks = keyed + pause + blips + pause
    env = [float(m) if i < len(keyed) else 0.0 for i, m in enumerate(marks)]
    env = [x + rng.gauss(0.0, GATE_SIGMA) for x in env]

    out = _gate(env, marks)
    assert out[: len(keyed)] == keyed
    assert sum(out[len(keyed):]) == 0
