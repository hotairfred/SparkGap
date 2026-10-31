"""Repeat-evidence spot rule (config spot_rule: "repeat"; cdub89/SparkGap#8 SP2).

CW Skimmer's rules: a call is spotted when it is the first callsign within LOOKAHEAD tokens
after CQ or TEST (or after DE when CQ or TEST came earlier and the call is sent twice:
CQ SKCC DE K4DH K4DH), copied exactly in tier(call) decode windows (2, 3 or 4 by patt3ch.lst)
near one frequency within the last REPEAT_S. A call followed by a name and a number is a
caller being sent the exchange and does not count. A call followed by DE and another call
(CQ S52Y DE N8EM) is the station being called and does not count either.

One vote on garbled decodes: a call does not spot while a call one edit from it has as many
copies or more near the same frequency (a tie waits for the next window). A decoder that
repeats its own busts (W6AYK for W6AYC) never lets the bust outrun the real call; nothing
is renamed, so near-identical real calls (N4VI next to N4ZZ) are not merged.

The spot goes out on the strongest window (bin SNR) among the copies, once per call and band per
REPEAT_S unless it moves more than MOVE_KHZ. A window is one decode window of one bin;
both LPF paths of a window count once. One transmission counts once: when two bins sit on
one signal (100 Hz apart), a copy of the call from the second bin less than one window length
after the first is the same audio and is no copy; the stronger of the two gives the frequency
and the SNR (spot_snr).

CQ groups (cq_groups; always on in SparkGap): every CQ group naming the call
counts as a copy, also several in one window (CQ WES DE N5QLZ N5QLZ K CQ WES DE N5QLZ is two),
so a call sent in two or more CQs of one window does not wait for the next window. The two
LPF paths of a window are not added: the larger count of the two is used.

CQ qualifiers (cq_qualifiers; always on in SparkGap): a word of 2 to 5 letters
right after CQ or TEST (CQ PA, CQ FD, CQ WES) is the window's CQ qualifier and is no exchange
name wherever it appears in that window, and a stop word is no number or state. Without it
CQ PA WB3EML PA CQ reads PA CQ as name and state, and WB3EML as a caller.

Lock-on (lockon; always on in SparkGap): a call named by a CQ group owns the
frequency it was copied on. While it owns it, two things change near that frequency:
  - the owner's own later sign-offs (TU W6YH, DE W6YH, W6YH TEST) count as copies of W6YH,
    so a runner who CQs once and then signs with TU W6YH still repeats; a bare W6YH does not,
    and nor does a sign-off followed by stray letters that complete a longer call (KF2A T K,
    DE WA1E CE): the owner may be a fragment of a call the decoder split at a gap;
  - a bare TEST right after the owner's call (TU W6YH TEST EI4KF) names nobody: it is the
    owner signing, and EI4KF is the next caller, even when its exchange is garbled or in cut
    numbers (5NN AT) the exchange test does not read.
  - a CQ group naming another call does not count while the owner's call is in the same
    window: CQ AZQP K6NR decoded as CQ A6QP K6NR, or TU K6NR ... CQ K6T, is the owner garbled.
    In windows without the owner it counts as usual, and the frequency passes to that call
    once it has tier(call) copies there.
Ownership lapses after LOCK_IDLE_S without a copy of the owner's call there. A call never
named by a CQ group never owns a frequency, so TU <caller> cannot promote a caller.

Doubled calls (doubled; off by default): a call sent twice inside one CQ group
(CQ K8TLJ K8TLJ, CQ CQ DE K8TLJ K8TLJ K) counts as two copies, so a common call spots on its
first clean CQ. Needs cq_groups. Whether CW Skimmer counts this way, and whether RBN wants it,
is open (hotairfred/SparkGap#43); on 7 recordings it added 101 calls, 89 confirmed.

Runner signs (runner_signs; off by default; cdub89/SparkGap#37): split DX often never sends CQ
(JH7ME 5NN TU UP KH8A UP). KH8A UP or TU UP KH8A makes KH8A the split runner of that frequency
for REPEAT_S; each window there holding KH8A whole counts as a copy, as a CQ group would, even
with its UP garbled (KH8A TP), so it spots on tier(call) such windows. A call sent a report
near that frequency (JH7ME 5NN) never counts, and a station calling on the DX's frequency never
gets a runner sign. TU <call> is no sign (in a contest the next caller follows TU at once). It
adds copies only: it owns nothing.
"""

import re
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass

KEYWORDS = frozenset({"CQ", "TEST"})
CONTEST_WORDS = frozenset({"CWT", "SST", "MST"})   # contest_words: open a group when the call follows
LOOKAHEAD = 3                 # tokens after a keyword searched for the sender's call
DE_LOOKBACK = 6               # tokens before DE searched for CQ or TEST
NEAR_KHZ = 0.5                # copies within this of each other are one frequency
REPEAT_S = 600.0              # copies count for 10 minutes; one spot per call and band per 10 minutes
MOVE_KHZ = 1.0                # ...unless it moves more than this (CW Skimmer re-spots 1 kHz moves)
OWNER_CELL_KHZ = 0.25         # owner_cell: a new owner replaces only the owner within this
STRONGEST_S = 60.0            # strongest: a stronger copy of the call elsewhere in the band this recently
LOCK_IDLE_S = 180.0           # lock-on: ownership lapses after this long without the owner's call

CALL_RE = re.compile(r"^(?:[A-Z0-9]{1,3}/)?"                      # HK3/
                     r"(?:[A-Z]{1,2}|[0-9][A-Z])[0-9]{1,4}[A-Z]{1,6}"
                     r"(?:/[A-Z0-9]{1,3})?$")                          # /0, /P
_SPLIT_RE = re.compile(r"[^A-Z0-9?/]+")   # keep / for HK3/NP4Z, N5AW/0
_RST_RE = re.compile(r"^[5E][9N][9N]$")    # 599, 5NN, ENN: contest report before the number
GRID_RE = re.compile(r"^(?!RR73$)[A-R]{2}[0-9]{2}$")  # a 4-character grid square: the exchange of
#                                                      Makrothen RTTY; RR73 (a sign-off) has the shape but is not one
_MERGED_PREFIXES = ("CQ", "TEST", "CWT", "SST", "MST", "DE")
_TRIGGER_WORDS = ("CQ", "TEST", "CWT", "SST", "MST")
_STOP_WORDS = frozenset({"TU", "DE", "CQ", "TEST", "CWT", "SST", "MST", "QRZ", "AGN",
                         "NR", "TNX", "GL", "BK"})
_PROSIGNS = frozenset({"K", "AR", "KN", "BK", "SK", "TU", "EE", "DX", "UP", "NA", "GM", "GE", "GA",
                       "GN", "ES", "HW", "CL", "OK", "RR", "UR", "OP", "VY", "FB", "HI"})
STRAY_MAX = 2                 # letters after a call that may be the rest of it
UP_NOISE_MAX = 2              # runner_signs: stray letters allowed between TU UP and the call after it
_NOISE_LETTERS = frozenset("ETIS?")                          # la1d: noise between CQ and the call
_SIGNOFF = frozenset({"K", "AR", "KN", "SK", "BK", "TU"})    # la1d: ends the search for the sender

# Validation (spot_validation): a call spots when its confidence score reaches the level's
# threshold, in place of the tier count. Weights are a logistic fit on the three legs of the
# 10-12..10-14 dry run (cdub89/SparkGap#10), every spot checked against RBN; thresholds from
# the curve with each leg scored by weights fitted on the other two.
VALIDATION = {"permissive": 1.25, "normal": 3.0, "aggressive": 5.0}
_WEIGHTS = {"bias": 0.74, "single": -1.49, "three": 0.43, "four_plus": 0.42, "windows": 1.68,
            "tier3": -0.52, "tier4": -1.82, "heard_before": 1.87, "paths_agree": 0.65,
            "paths_disagree": -0.63, "doubled": 2.48, "rival": -1.64}
_WEIGHTS_LISTED = {           # per call list (spot_validation_calls): scp, rbn, rbn+skcc
    "scp": {"bias": 0.29, "single": -1.52, "three": 0.42, "four_plus": 0.28, "windows": 1.58,
            "tier3": -0.27, "tier4": -1.46, "heard_before": 1.68, "paths_agree": 0.48,
            "paths_disagree": -0.70, "doubled": 2.25, "rival": -1.62, "listed": 2.48},
    "rbn": {"bias": -0.22, "single": -1.57, "three": 0.24, "four_plus": 0.50, "windows": 1.30,
            "tier3": 0.0, "tier4": -1.90, "heard_before": 1.45, "paths_agree": 0.56,
            "paths_disagree": -0.79, "doubled": 1.90, "rival": -0.88, "listed": 4.18},
    "rbn+skcc": {"bias": -0.60, "single": -1.58, "three": 0.38, "four_plus": 0.72, "windows": 1.35,
                 "tier3": -0.05, "tier4": -1.55, "heard_before": 1.44, "paths_agree": 0.28,
                 "paths_disagree": -0.72, "doubled": 1.77, "rival": -0.89, "listed": 3.92},
}
HEARD_KEEP_S = 12 * 3600.0    # validation: a call unheard this long is forgotten
_MORSE = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/",
                  (".- -... -.-. -.. . ..-. --. .... .. .--- -.- .-.. -- -. --- .--. --.- .-. ... - "
                   "..- ...- .-- -..- -.-- --.. ----- .---- ..--- ...-- ....- ..... -.... --... ---.. "
                   "----. -..-.").split(), strict=True))


def _split_run_together(rest: str, prev: str | None) -> list[str] | None:
    """The whole of rest as keywords, an event tag and a call, else None. A tag (2-5
    letters, no digit, so never a call) only follows CQ or TEST, and only DE or the end
    of the word follows it: CQCQDE, CQCQWESDE, CQPADEW3RA."""
    if not rest:
        return []
    if prev is not None and CALL_RE.match(rest):
        return [rest]
    for pre in sorted(_MERGED_PREFIXES + ("TU",), key=len, reverse=True):
        if rest.startswith(pre):
            tail = _split_run_together(rest[len(pre):], pre)
            if tail is not None:
                return [pre] + tail
    if prev in ("CQ", "TEST"):
        cuts = range(min(5, len(rest)), 1, -1)
        for n in [n for n in cuts if rest[n:].startswith("DE")] + [n for n in cuts if n == len(rest)]:
            tag, after = rest[:n], rest[n:]
            if tag.isalpha():
                tail = _split_run_together(after, tag)
                if tail is not None:
                    return [tag] + tail
    return None


def _split_merged(tok: str) -> list[str]:
    """CQK1ABC -> CQ K1ABC, DEW1AW -> DE W1AW (decoder dropped the word gap)."""
    out: list[str] = []
    rest = tok
    while rest:
        for pre in _MERGED_PREFIXES:
            if (rest.startswith(pre) and len(rest) > len(pre)
                    and re.match(r"[A-Z0-9]{1,2}\d", rest[len(pre):])):
                out.append(pre)
                rest = rest[len(pre):]
                break
        else:
            out.append(rest)
            rest = ""
    return out


def split_runs(text: str) -> str:
    """Text with words run together split apart (spot_rule_split_runs): hand-sent CW often
    drops the word gap, CQCQDE -> CQ CQ DE, CQPADEW3RA -> CQ PA DE W3RA. A word is split only
    when the whole of it reads as keywords, an event tag and a call; anything else is kept."""
    out = []
    for w in text.split():
        u = w.upper()
        parts = None if CALL_RE.match(u) else _split_run_together(u, None)
        out.extend(parts if parts and len(parts) > 1 else [w])
    return " ".join(out)


def tokens(text: str, gluek: bool = False) -> list[str]:
    """Upper-case word tokens of decoded text, merged keyword prefixes split off. gluek
    (spot_rule_gluek): a token CALL+K whose CALL (4 or more characters) is itself a token of
    the text reads as CALL K, the K prosign glued to the call (WB0MQA ... WB0MQAK)."""
    out: list[str] = []
    for t in _SPLIT_RE.split(text.upper()):
        t = t.strip("/")
        if t:
            out.extend(_split_merged(t))
    if gluek:
        present = set(out)
        split: list[str] = []
        for t in out:
            c = t[:-1]
            if t.endswith("K") and len(c) >= 4 and c in present and CALL_RE.match(c):
                split.extend([c, "K"])
            else:
                split.append(t)
        return split
    return out


def _one_sub_same_kind(a: str, b: str) -> bool:
    """One substitution, letter for letter or digit for digit."""
    if len(a) != len(b):
        return False
    diff = [(x, y) for x, y in zip(a, b, strict=True) if x != y]
    return len(diff) == 1 and diff[0][0].isalpha() == diff[0][1].isalpha()


def _qualifiers(toks: list[str]) -> set[str]:
    """CQ qualifiers of a window: 2-5 letters right after CQ or TEST (CQ PA, CQ FD, CQ DX)."""
    return {b for a, b in zip(toks, toks[1:]) if a in KEYWORDS and b.isalpha()
            and 2 <= len(b) <= 5 and b not in _STOP_WORDS}


def _is_trigger_like(t: str) -> bool:
    return t in _TRIGGER_WORDS or any(_one_sub_same_kind(t, w) for w in ("CWT", "TEST"))


def sender(toks: list[str], i: int, la1d: bool = False) -> int | None:
    """Index of the first callsign within LOOKAHEAD tokens after keyword toks[i]. la1d
    (spot_rule_la1d): a single noise letter (E T I S ?) takes no LOOKAHEAD slot and a sign-off
    prosign (K AR KN SK BK TU) ends the search: CQ CQ E E HFT ON6FT names ON6FT, CQ K W1AW
    names nobody (a finished CQ, W1AW is the caller)."""
    seen = 0
    for j in range(i + 1, len(toks)):
        t = toks[j]
        if la1d and len(t) == 1 and t in _NOISE_LETTERS:
            continue
        if CALL_RE.match(t):
            return j
        if la1d and t in _SIGNOFF:
            return None
        seen += 1
        if seen >= LOOKAHEAD:
            return None
    return None


def is_exchange(toks: list[str], j: int, qualifiers: bool = False, grid: bool = False) -> bool:
    """toks[j] is followed by NAME then a number or 2-letter state, or by a report (5NN) then
    a number: a caller being sent the exchange. Repeats of the call and 1-character or '?'
    tokens before NAME are skipped. qualifiers (spot_rule_cq_qualifiers): a CQ qualifier of
    the window is no NAME and a stop word is no number or state (CQ PA WB3EML PA CQ).
    grid (a contest whose exchange is a grid square): the call followed by a grid square is
    being sent it (CQ MAKRO NK2Y NK2Y CQ AA4DD AA4DD FN31: AA4DD answering NK2Y)."""
    k = j + 1
    while k < len(toks) and (toks[k] == toks[j] or len(toks[k]) <= 1 or "?" in toks[k]):
        k += 1
    if grid and k < len(toks) and GRID_RE.match(toks[k]):
        return True
    if k + 1 >= len(toks):
        return False
    name, num = toks[k], toks[k + 1]
    if _RST_RE.match(name):                      # CQ WW, WPX: report then zone or serial
        return any(ch.isdigit() for ch in num)
    if qualifiers and (name in _qualifiers(toks) or num in _STOP_WORDS):
        return False
    return (name.isalpha() and 2 <= len(name) <= 6 and name not in _STOP_WORDS
            and not _is_trigger_like(name)
            and (any(ch.isdigit() for ch in num) or (num.isalpha() and len(num) == 2)))


def _cq_before(toks: list[str], i: int) -> bool:
    """CQ or TEST within DE_LOOKBACK tokens before toks[i], with no callsign between."""
    for k in range(i - 1, max(i - 1 - DE_LOOKBACK, -1), -1):
        if CALL_RE.match(toks[k].replace("?", "")):
            return False
        if toks[k] in KEYWORDS:
            return True
    return False


def runner_calls(text: str, every: bool = False, qualifiers: bool = False,
                 doubled: bool = False, split_up: bool = False,
                 contest_words: bool = False, cq_repeats: bool = False,
                 stray: bool = False, words: frozenset[str] | None = None,
                 grid: bool = False, la1d: bool = False, gluek: bool = False) -> list[str]:
    """Senders of CQ groups (rules 1 and 4). A group opens at CQ or TEST, or at DE after a CQ.
    Its sender is the call inside it or right after the keywords (within LOOKAHEAD). Keywords
    right after the sender close the group; the next transmission (usually a caller) starts
    after them: CQ TEST W6YH, CQ W6YH TEST, TEST W6YH, CQ TEST W6YH TEST, and the call sent
    twice (CQ TEST W6YH W6YH TEST). TU W6YH and a bare W6YH TEST do not name W6YH: on the 40m
    CWT 10-08 and 20m CWT 09-23 they promoted callers (+28 and +18 calls CW Skimmer did not
    send, for 3 more runners). The TEST of a bare W6YH TEST still opens a group on the call
    after it; only the exchange test keeps that caller out. every: one entry per group
    instead of one per call."""
    return [call for call, _after in runner_groups(text, every, qualifiers, doubled, split_up,
                                                   contest_words, cq_repeats, stray, words, grid,
                                                   la1d, gluek)]


def _call_before(toks: list[str], i: int) -> str | None:
    """The callsign right before toks[i] (1-character and '?' tokens skipped), else None."""
    k = i - 1
    while k >= 0 and (len(toks[k]) <= 1 or "?" in toks[k]):
        k -= 1
    return toks[k] if k >= 0 and CALL_RE.match(toks[k]) else None


def _signing(toks: list[str], k: int, qualifiers: bool = False, grid: bool = False) -> bool:
    """toks[k] is a call in a sign-off: after TU or DE, or before CQ, TEST or another trigger
    word (1-character and '?' tokens skipped), and not being sent the exchange. A bare repeat
    of the call does not count: a fragment picked once can repeat on its own."""
    if is_exchange(toks, k, qualifiers, grid):
        return False
    p = k - 1
    while p >= 0 and (len(toks[p]) <= 1 or "?" in toks[p]):
        p -= 1
    n = k + 1
    while n < len(toks) and (len(toks[n]) <= 1 or "?" in toks[n]):
        n += 1
    return ((p >= 0 and toks[p] in ("TU", "DE"))
            or (n < len(toks) and toks[n] in _TRIGGER_WORDS))


def _stray_letters(toks: list[str], call: str, k: int) -> bool:
    """toks[k:] opens with up to STRAY_MAX stray letters, over one or two tokens, that complete a
    longer call: W9ME T, K0D CR, W9M E T. A keyword, stop word or prosign (K, AR, TU) ends the
    call cleanly."""
    joined, m = call, k
    while m < len(toks) and len(joined) - len(call) < STRAY_MAX:
        t = toks[m]
        if (not t.isalpha() or len(joined) - len(call) + len(t) > STRAY_MAX
                or t in _PROSIGNS or t in _STOP_WORDS or t in KEYWORDS):
            break
        joined += t
        m += 1
    return len(joined) > len(call) and CALL_RE.match(joined) is not None


def _split_signoff(toks: list[str], i: int) -> int | None:
    """toks[i] is TU and the next word is a call followed directly by UP (TU CY0S UP): a station
    working split signing off. Returns the call's index, else None."""
    j = i + 1
    if (toks[i] == "TU" and j + 1 < len(toks) and CALL_RE.match(toks[j]) is not None
            and toks[j + 1] == "UP"):
        return j
    return None


def runner_signs_of(text: str, gluek: bool = False) -> tuple[set[str], set[str]]:
    """Runner signs of one decoded text (runner_signs): the calls sent as a split runner, KH8A UP
    or TU UP KH8A (stray letters after UP allowed: TU UP E KH8A; a bare UP before a call is
    noise as often as not: G U? UP E KD2SC), and the calls sent a report,
    JH7ME 5NN, which are the stations being worked and never the runner. A DX working split
    never sends CQ: JH7ME 5NN TU UP KH8A UP. TU <call> is no sign: in a contest the next caller
    follows the runner's TU at once (TU KK4E KK4E)."""
    toks = tokens(text, gluek)
    runners: set[str] = set()
    worked: set[str] = set()
    for i, t in enumerate(toks):
        if CALL_RE.match(t) is None:
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        before = [x for x in toks[max(0, i - 2 - UP_NOISE_MAX):i] if x not in _NOISE_LETTERS]
        if _RST_RE.match(nxt):
            worked.add(t)
        elif nxt == "UP" or before[-2:] == ["TU", "UP"]:
            runners.add(t)
    return runners - worked, worked


def runner_groups(text: str, every: bool = False, qualifiers: bool = False,
                  doubled: bool = False, split_up: bool = False, contest_words: bool = False,
                  cq_repeats: bool = False, stray: bool = False, words: frozenset[str] | None = None,
                  grid: bool = False, la1d: bool = False,
                  gluek: bool = False) -> list[tuple[str, str | None]]:
    """runner_calls() with, for each call, the callsign right before a bare TEST that opened
    its group (TU W6YH TEST EI4KF -> ("EI4KF", "W6YH")), else None. Lock-on uses it: when W6YH
    owns the frequency, that TEST is W6YH signing and EI4KF is a caller.
    contest_words: CWT, SST or MST right before a call opens a group as CQ does (CWT K5AX CWT
    K5AX: runners who drop the CQ). cq_repeats: a closing CQ or TEST followed by the same call
    opens the next group instead of closing this one, so CQ CWT N1DC CQ CWT N1DC is two
    copies, not one. words: the contest words for contest_words (default CWT, SST, MST); grid:
    a call followed by a grid square is being sent the exchange (is_exchange). la1d, gluek: see
    sender() and tokens()."""
    toks = tokens(text, gluek)
    cwords = CONTEST_WORDS if words is None else words
    out: list[tuple[str, str | None]] = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if split_up:
            j = _split_signoff(toks, i)
            if j is not None:
                if every or all(toks[j] != c for c, _ in out):
                    out.append((toks[j], None))
                i = j + 2
                continue
        de = t == "DE" and _cq_before(toks, i)
        cw = (contest_words and t in cwords and i + 1 < n and len(toks[i + 1]) >= 4
              and CALL_RE.match(toks[i + 1]) is not None)     # not 3-character pieces (N0K, K1S)
        if not (t in KEYWORDS or de or cw):
            i += 1
            continue
        j = sender(toks, i, la1d)
        if j is None or (de and toks.count(toks[j]) < 2):
            i += 1
            continue
        k = j + 1
        while k < n and toks[k] == toks[j]:       # call sent twice before the closing keyword
            k += 1
        if t == "CQ" and j == i + 1 and k < n and toks[k] == "DE":
            j2 = sender(toks, k, la1d)
            if j2 is not None and toks[j2] != toks[j]:
                i = k                             # CQ S52Y DE N8EM: S52Y is being called; after
                                                  # TEST or DE the call is the runner's
                continue
        if (not is_exchange(toks, j, qualifiers, grid) and not (stray and _stray_after(toks, j))
                and (every or all(toks[j] != c for c, _ in out))):
            out.append((toks[j], _call_before(toks, i) if t == "TEST" else None))
            if doubled and every:
                out.extend([out[-1]] * (k - j - 1))   # doubled: each copy counts
        i = k
        while i < n and toks[i] in KEYWORDS:      # closing keywords end the group
            if cq_repeats and sender(toks, i, la1d) is not None and toks[sender(toks, i, la1d)] == toks[j]:
                break                             # ... unless the same call follows: a new CQ
            i += 1
    return out


def _lev1(a: str, b: str) -> bool:
    """True when a and b are exactly one edit apart."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _stray_after(toks: list[str], j: int) -> bool:
    """The call toks[j] is followed (past repeats of it) by one or two single letters that
    complete a longer call: TEST K1V U T is K1VUT split at a gap. K is a prosign, not a letter,
    and the first letter is no E or T (CQ TEST K4TZ E E is K4TZ and noise). Only calls of 3
    characters are checked: longer ones are followed by their own pieces (W0UI W 0 UI)."""
    if len(toks[j]) > 3:
        return False
    k = j + 1
    while k < len(toks) and toks[k] == toks[j]:
        k += 1
    joined = toks[j]
    for m, t in enumerate(toks[k:k + 2]):
        if len(t) != 1 or not t.isalpha() or t == "K" or (m == 0 and t in "ET"):
            break
        joined += t
        if CALL_RE.match(joined) and len(joined) <= 7:
            return True
    return False


def _fragment(short: str, long_: str) -> bool:
    """short is a piece of the longer call long_: its start, its end, or inside it, 2 to 4
    characters shorter (K1V of K1VUT, N6T of N6TQS). One character shorter is the one-edit
    vote's and the rival term's case already."""
    if short in long_.split("/") or long_.startswith(short + "/") or long_.endswith("/" + short):
        return False      # WA2USA is the call of WA2USA/P, HK3/NP4Z of HK3/NP4Z/P: not pieces
    d = len(long_) - len(short)
    return 2 <= d <= 4 and len(short) >= 2 and short in long_ and CALL_RE.match(long_) is not None


def _regap(a: str, b: str) -> bool:
    """a and b are one keying read with the letter gaps in different places (C0TT, NN0TT), or
    one's Morse elements start the other's (N7HR, N7HRK/M)."""
    if a == b:
        return False
    ea, eb = ("".join(_MORSE.get(ch, "?") for ch in c) for c in (a, b))
    return ea.startswith(eb) or eb.startswith(ea)


@dataclass(frozen=True)
class _Copy:
    time: float
    window: int
    call: str
    freq_khz: float
    snr: float
    nth: int = 0              # which CQ group of its window (always 0 without cq_groups)
    report: float | None = None   # the SNR a spot from this copy reports, when it differs from snr


class RepeatSpotRule:
    """Feed every decode window; returns the spots the window completes."""

    def __init__(self, tier: Callable[[str], int], cq_groups: bool = False,
                 lockon: bool = False, split_runs: bool = False,
                 cq_qualifiers: bool = False, doubled: bool = False, split_up: bool = False,
                 window_s: float = 60.0,
                 validation: str | None = None,
                 listed: Callable[[str], bool] | None = None, listed_by: str = "scp",
                 contest_words: bool = False, cq_repeats: bool = False,
                 fragments: str | None = None, words: frozenset[str] | None = None,
                 grid: bool = False, owner_cell: bool = False,
                 owned_two_windows: bool = False, lock_idle_s: float = LOCK_IDLE_S,
                 owner_listed: bool = False, la1d: bool = False, gluek: bool = False,
                 norival: bool = False, strongest_db: float | None = None,
                 takeover_idle_s: float | None = None, runner_signs: bool = False) -> None:
        if validation is not None and validation not in VALIDATION:
            raise ValueError(f"validation must be one of {list(VALIDATION)}, got {validation!r}")
        if listed_by not in _WEIGHTS_LISTED:
            raise ValueError(f"listed_by must be one of {list(_WEIGHTS_LISTED)}, got {listed_by!r}")
        self.tier = tier
        self.threshold = VALIDATION[validation] if validation else None
        self.listed = listed                                    # validation: call list lookup, or None
        self.weights = _WEIGHTS_LISTED[listed_by] if listed else _WEIGHTS
        self._paths: dict[int, list[Counter]] = {}              # validation: window -> token counts per path
        self._heard: dict[str, list[float]] = {}                # validation: call -> [first, last copy]
        self.window_s = window_s                                # decode window length
        self.cq_groups = cq_groups
        self.lockon = lockon
        self.split_runs = split_runs
        self.cq_qualifiers = cq_qualifiers
        self.doubled = doubled and validation is None           # validation scores a doubled call itself
        self.split_up = split_up          # TU <call> UP (a station working split) counts as a copy
        self.contest_words = contest_words      # CWT K5AX opens a group as CQ CWT K5AX does
        self.cq_repeats = cq_repeats            # CQ CWT N1DC CQ CWT N1DC is two copies
        self.words = words                      # contest_words: the contest's words (None: CWT, SST, MST)
        self.grid = grid                        # a call followed by a grid square is a caller
        if fragments is not None and not isinstance(fragments, str):
            raise ValueError(f"fragments takes 'even' or 'any', and/or 'stray', got {fragments!r}")
        opts = set((fragments or '').split(',')) - {''}
        if not opts <= {'even', 'any', 'stray'} or {'even', 'any'} <= opts:
            raise ValueError(f"fragments takes 'even' or 'any', and/or 'stray', got {fragments!r}")
        self.fragments = 'even' if 'even' in opts else 'any' if 'any' in opts else None
        self.stray = 'stray' in opts                            # TEST K1V U T: no copy of K1V
        self.owner_cell = owner_cell            # a neighbour 0.5 kHz away keeps its frequency
        self.owned_two_windows = owned_two_windows   # a challenger at an owned frequency needs 2 windows
        self.lock_idle_s = lock_idle_s          # lock-on: ownership lapses after this long
        self.takeover_idle_s = takeover_idle_s  # ...but another call may take the frequency after this long
        self.owner_listed = owner_listed        # one copy owns an empty frequency only when listed
        self.la1d = la1d                        # sender(): noise letters and sign-offs, see sender()
        self.gluek = gluek                      # tokens(): CALL+K reads as CALL K, see tokens()
        self.norival = norival                  # score(): no rival penalty for a listed call from an unlisted rival
        self.strongest_db = strongest_db        # a copy this many dB stronger elsewhere in the band: no spot here
        self.runner_signs = runner_signs        # KH8A UP: KH8A runs here, see _runner_copies()
        self._worked: deque[tuple[float, str, float]] = deque()       # runner_signs: (time, call, freq)
        self._runners: deque[tuple[float, str, float]] = deque()      # runner_signs: (time, call, freq)
        self._owners: list[list] = []                           # lock-on: [call, freq_khz, last seen]
        self._copies: deque[_Copy] = deque()                    # oldest first
        self._seen: set[tuple[int, str, int]] = set()           # (window, call, nth) in _copies
        self._window_ids: dict[tuple[int, int], tuple[int, float]] = {}   # -> (id, first seen)
        self._bins: dict[int, int] = {}                         # window id -> bin
        self._next_window = 0
        self._last_spot: dict[tuple[str, int], tuple[float, float]] = {}   # (call, MHz) -> (time, freq)
        self.spot_snr: dict[str, float] = {}                    # call -> SNR of the copy that gave its last spot
        self._pruned = 0.0

    def window_id(self, bin_id: int, window_key: int, now: float) -> int:
        """Stable id for one bin's decode window (both LPF paths map to the same id)."""
        key = (bin_id, window_key)
        if key not in self._window_ids:
            self._window_ids[key] = (self._next_window, now)
            self._bins[self._next_window] = bin_id
            self._next_window += 1
        return self._window_ids[key][0]

    def feed(self, window: int, freq_khz: float, text: str, now: float,
             snr: float | None = None, report: float | None = None) -> list[tuple[str, float]]:
        """Record one decoded text of a window; return [(call, freq_khz)] to spot now. snr ranks the
        copies (best frequency, same audio); report, when given, is the SNR the spot
        carries (itila2_snr_decoder), from the copy snr chose, else that call's latest copy with one."""
        self._expire(now)
        owned = self.owner(freq_khz, now, self.takeover_idle_s) if self.owned_two_windows else None
        if self.split_runs:
            text = split_runs(text)
        if self.threshold is not None:
            self._paths.setdefault(window, []).append(Counter(tokens(text, self.gluek)))
        spots = []
        calls = (self._lockon_calls(freq_khz, text, now) if self.lockon
                 else runner_calls(text, every=self.cq_groups, qualifiers=self.cq_qualifiers,
                                   doubled=self.doubled, split_up=self.split_up,
                                   contest_words=self.contest_words, cq_repeats=self.cq_repeats,
                                   stray=self.stray, words=self.words, grid=self.grid,
                                   la1d=self.la1d, gluek=self.gluek))
        if self.runner_signs:
            calls = calls + self._runner_copies(window, freq_khz, text, now, calls)
        for call, n in Counter(calls).items():
            new = 0
            for nth in range(n):
                if (window, call, nth) not in self._seen:
                    if self._same_audio(window, call, freq_khz, now, snr, report):
                        continue
                    self._seen.add((window, call, nth))
                    self._copies.append(_Copy(now, window, call, freq_khz,
                                              snr if snr is not None else float("-inf"), nth, report))
                    new += 1
            near = [c for c in self._copies if abs(c.freq_khz - freq_khz) <= NEAR_KHZ]
            counts = Counter(c.call for c in near)
            if self.threshold is not None:
                heard = self._heard.setdefault(call, [now, now])
                heard[1] = now
                if not new or self.score(call, near, counts, now - heard[0] >= REPEAT_S) < self.threshold:
                    continue
            elif any(_lev1(other, call) and n >= counts[call] for other, n in counts.items()):
                continue          # a call one edit away has as many copies here: undecided
            elif counts[call] < self.tier(call):
                continue
            if self.fragments and any(_fragment(call, other)
                                      and k >= (counts[call] if self.fragments == 'even' else 1)
                                      for other, k in counts.items()):
                continue          # a piece of a longer call heard here (K1V of K1VUT): wait
            if owned is not None and owned != call and len({c.window for c in near if c.call == call}) < 2:
                continue          # another call owns this frequency: a caller read as the runner waits
            best = max((c for c in near if c.call == call), key=lambda c: c.snr)
            spot_f = best.freq_khz
            if self.strongest_db is not None and self._stronger_elsewhere(best, now):
                continue          # a spur or image of a stronger signal of the same call
            key = (call, int(spot_f // 1000))     # per band: a station running two bands
            last = self._last_spot.get(key)       # (multi-op, SO2R, 2BSIQ) is not re-spotted at each switch
            if last is None or now - last[0] >= REPEAT_S or abs(spot_f - last[1]) >= MOVE_KHZ:
                self._last_spot[key] = (now, spot_f)
                best = max((c for c in near if c.call == call), key=lambda c: c.snr)   # the copy spot_f came from
                reports = [c.report for c in near if c.call == call and c.report is not None]
                if best.report is not None:
                    self.spot_snr[call] = best.report
                elif reports:
                    self.spot_snr[call] = reports[-1]
                elif best.snr != float("-inf"):        # a copy fed without an SNR keeps the window's
                    self.spot_snr[call] = best.snr
                else:
                    self.spot_snr.pop(call, None)
                spots.append((call, spot_f))
        return spots

    def _stronger_elsewhere(self, best: _Copy, now: float) -> bool:
        """strongest_db: a copy of best's call in the same band, more than MOVE_KHZ away, read in
        the last STRONGEST_S at least strongest_db stronger. Copies without an SNR never count."""
        if best.snr == float("-inf") or self.strongest_db is None:
            return False
        band = int(best.freq_khz // 1000)
        return any(c.call == best.call and int(c.freq_khz // 1000) == band
                   and abs(c.freq_khz - best.freq_khz) > MOVE_KHZ and now - c.time <= STRONGEST_S
                   and c.snr >= best.snr + self.strongest_db for c in self._copies)

    def score(self, call: str, near: list[_Copy], counts: Counter, heard_before: bool) -> float:
        """Validation: confidence that call is what was sent, from its copies near one frequency."""
        w = self.weights
        n = counts[call]
        windows = {c.window for c in near if c.call == call}
        paths = [self._paths.get(win, []) for win in windows]
        both = [all(p[call] for p in ps) for ps in paths if len(ps) > 1]
        agree = sum(both) / len(both) if both else None
        tier = self.tier(call)
        rivals = [other for other, k in counts.items()
                  if k >= 2 and other != call and (_lev1(other, call) or _regap(call, other))]
        rival = bool(rivals)
        if rival and self.norival and self.listed and self.listed(call) and not any(self.listed(o) for o in rivals):
            rival = False                       # norival: an unlisted rival reading does not count against a listed call
        s = (w["bias"] + w["single"] * (n == 1) + w["three"] * (n == 3) + w["four_plus"] * (n >= 4)
             + w["windows"] * min(len(windows), 5) / 5
             + w["tier3"] * (tier == 3) + w["tier4"] * (tier == 4) + w["heard_before"] * heard_before
             + w["paths_agree"] * (agree == 1.0) + w["paths_disagree"] * (agree is not None and agree < 0.5)
             + w["doubled"] * any(p[call] >= 2 for ps in paths for p in ps)
             + w["rival"] * rival)
        if self.listed:
            s += w["listed"] * self.listed(call)
        return s

    def _same_audio(self, window: int, call: str, freq_khz: float, now: float,
                    snr: float | None, report: float | None = None) -> bool:
        """Another bin gave this call near freq_khz less than one window length ago. That copy
        takes this bin's frequency when this bin is the stronger."""
        mine = self._bins.get(window)
        if mine is None:
            return False
        for i, c in enumerate(self._copies):
            if (c.call == call and abs(c.freq_khz - freq_khz) <= NEAR_KHZ
                    and now - c.time < self.window_s and self._bins.get(c.window, mine) != mine):
                if snr is not None and snr > c.snr:
                    self._copies[i] = _Copy(c.time, c.window, call, freq_khz, snr, c.nth,
                                            report if report is not None else c.report)
                return True
        return False

    def owner(self, freq_khz: float, now: float, idle_s: float | None = None) -> str | None:
        """Lock-on: the call owning the frequency nearest freq_khz (within NEAR_KHZ), else None.
        idle_s: how long ownership lasts without the owner's call (default lock_idle_s)."""
        idle = self.lock_idle_s if idle_s is None else idle_s
        live = [o for o in self._owners
                if abs(o[1] - freq_khz) <= NEAR_KHZ and now - o[2] <= idle]
        return min(live, key=lambda o: abs(o[1] - freq_khz))[0] if live else None

    def _own(self, call: str, freq_khz: float, now: float) -> None:
        cell = OWNER_CELL_KHZ if self.owner_cell else NEAR_KHZ
        self._owners = [o for o in self._owners
                        if abs(o[1] - freq_khz) > cell and now - o[2] <= self.lock_idle_s]
        self._owners.append([call, freq_khz, now])

    def _glued(self, call: str, freq_khz: float) -> bool:
        """owner_listed: call is a copy heard near freq_khz with a stop word or prosign glued to
        it (W4E DE read as W4EDE, WB0MQA K as WB0MQAK): a garble of that call, never an owner."""
        return any(call.startswith(c.call) and call[len(c.call):] in _STOP_WORDS | _PROSIGNS
                   and abs(c.freq_khz - freq_khz) <= NEAR_KHZ for c in self._copies)

    def _truer_call(self, call: str, owner: str) -> bool:
        """call is what a garbled owner should have read: one edit from it and in a more trusted
        tier (W3IO, an active call, against BW3IO, unknown). Not a mere part of it: W2L inside
        W2LCQ/NLI is a fragment of the real call."""
        return _lev1(call, owner) and self.tier(call) < self.tier(owner)

    def _lockon_calls(self, freq_khz: float, text: str, now: float) -> list[str]:
        """Calls one decoded text counts as copies of, with lock-on (see the module docstring)."""
        calls: list[str] = []
        toks = tokens(text, self.gluek)
        for call, after in runner_groups(text, every=self.cq_groups,
                                         qualifiers=self.cq_qualifiers, doubled=self.doubled,
                                         split_up=self.split_up, contest_words=self.contest_words,
                                         cq_repeats=self.cq_repeats, stray=self.stray,
                                         words=self.words, grid=self.grid,
                                         la1d=self.la1d, gluek=self.gluek):
            owner = self.owner(freq_khz, now, self.takeover_idle_s)   # who may block or be challenged
            if after is not None and after != call and owner == after:
                continue                          # the owner signing (W6YH TEST): call is a caller
            glued = self.owner_listed and self._glued(call, freq_khz)
            if owner is not None and call != owner:
                if owner in toks and not self._truer_call(call, owner):
                    continue                      # the owner is still here: CQ A6QP K6NR is a garble
                held = sum(1 for c in self._copies if c.call == call
                           and abs(c.freq_khz - freq_khz) <= NEAR_KHZ)
                calls.append(call)                # a challenger copy, in a window without the owner
                if held + 1 >= self.tier(call) and not glued:
                    self._own(call, freq_khz, now)
                continue
            calls.append(call)
            if owner is None and self.owner_listed and (glued or not (self.listed and self.listed(call))):
                held = sum(1 for c in self._copies if c.call == call
                           and abs(c.freq_khz - freq_khz) <= NEAR_KHZ)
                if glued or held + 1 < 2:
                    continue                      # owner_listed: one copy of an unlisted call owns nothing
            self._own(call, freq_khz, now)
        owner = self.owner(freq_khz, now)
        if owner is not None and owner not in calls:
            if any(tk == owner and _signing(toks, k, self.cq_qualifiers, self.grid)
                   and not _stray_letters(toks, owner, k + 1)
                   for k, tk in enumerate(toks)):
                calls.append(owner)               # TU W6YH, W6YH TEST: the owner signing
                self._own(owner, freq_khz, now)
        return calls

    def _runner_copies(self, window: int, freq_khz: float, text: str, now: float,
                       calls: list[str]) -> list[str]:
        """runner_signs: calls this window counts as copies of without a CQ. A runner sign (KH8A UP)
        makes the call the split runner near freq_khz for REPEAT_S; every window there holding
        the call whole then counts, even with its UP garbled (KH8A TP). A call sent a report
        near freq_khz never counts. The tier or the validation score still needs more windows."""
        runners, worked = runner_signs_of(text, self.gluek)
        self._worked.extend((now, c, freq_khz) for c in worked)
        self._runners.extend((now, c, freq_khz) for c in runners)
        near = {c for _, c, f in self._runners if abs(f - freq_khz) <= NEAR_KHZ}
        out: list[str] = []
        for c in tokens(text, self.gluek):
            if (c in near and c not in calls and c not in out
                    and not any(w == c and abs(f - freq_khz) <= NEAR_KHZ for _, w, f in self._worked)):
                out.append(c)
        return out

    def _expire(self, now: float) -> None:
        cutoff = now - REPEAT_S
        while self._copies and self._copies[0].time < cutoff:
            c = self._copies.popleft()
            self._seen.discard((c.window, c.call, c.nth))
        while self._worked and self._worked[0][0] < cutoff:
            self._worked.popleft()
        while self._runners and self._runners[0][0] < cutoff:
            self._runners.popleft()
        if now - self._pruned >= 60.0:
            self._pruned = now
            self._last_spot = {k: v for k, v in self._last_spot.items() if v[0] >= cutoff}
            self._window_ids = {k: v for k, v in self._window_ids.items() if v[1] >= cutoff}
            live = {v[0] for v in self._window_ids.values()}
            self._bins = {w: b for w, b in self._bins.items() if w in live}
            self._paths = {w: p for w, p in self._paths.items() if w in live}
            self._heard = {c: h for c, h in self._heard.items() if now - h[1] <= HEARD_KEEP_S}
