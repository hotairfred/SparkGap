"""Repeat-evidence spot rule (config spot_rule: "repeat"; cdub89/SparkGap#8 SP2).

CW Skimmer's rules: a call is spotted when it is the first callsign within LOOKAHEAD tokens
after CQ or TEST (or after DE when CQ or TEST came earlier and the call is sent twice:
CQ SKCC DE K4DH K4DH), copied exactly in tier(call) decode windows (2, 3 or 4 by patt3ch.lst)
near one frequency within the last REPEAT_S. A call followed by a name and a number is a
caller being sent the exchange and does not count.

One vote on garbled decodes: a call does not spot while a call one edit from it has as many
copies or more near the same frequency (a tie waits for the next window). A decoder that
repeats its own busts (W6AYK for W6AYC) never lets the bust outrun the real call; nothing
is renamed, so near-identical real calls (N4VI next to N4ZZ) are not merged.

The spot goes out on the strongest window (bin SNR) among the copies, once per call and band per
REPEAT_S unless it moves more than MOVE_KHZ. A window is one decode window of one bin;
both LPF paths of a window count once.

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
    so a runner who CQs once and then signs with TU W6YH still repeats; a bare W6YH does not;
  - a bare TEST right after the owner's call (TU W6YH TEST EI4KF) names nobody: it is the
    owner signing, and EI4KF is the next caller, even when its exchange is garbled or in cut
    numbers (5NN AT) the exchange test does not read.
  - a CQ group naming another call does not count while the owner's call is in the same
    window: CQ AZQP K6NR decoded as CQ A6QP K6NR, or TU K6NR ... CQ K6T, is the owner garbled.
    In windows without the owner it counts as usual, and the frequency passes to that call
    once it has tier(call) copies there.
Ownership lapses after LOCK_IDLE_S without a copy of the owner's call there. A call never
named by a CQ group never owns a frequency, so TU <caller> cannot promote a caller.
"""

import re
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass

KEYWORDS = frozenset({"CQ", "TEST"})
LOOKAHEAD = 3                 # tokens after a keyword searched for the sender's call
DE_LOOKBACK = 6               # tokens before DE searched for CQ or TEST
NEAR_KHZ = 0.5                # copies within this of each other are one frequency
REPEAT_S = 600.0              # copies count for 10 minutes; one spot per call and band per 10 minutes
MOVE_KHZ = 1.0                # ...unless it moves more than this (CW Skimmer re-spots 1 kHz moves)
LOCK_IDLE_S = 180.0           # lock-on: ownership lapses after this long without the owner's call

CALL_RE = re.compile(r"^(?:[A-Z0-9]{1,3}/)?"                      # HK3/
                     r"(?:[A-Z]{1,2}|[0-9][A-Z])[0-9]{1,4}[A-Z]{1,6}"
                     r"(?:/[A-Z0-9]{1,3})?$")                          # /0, /P
_SPLIT_RE = re.compile(r"[^A-Z0-9?/]+")   # keep / for HK3/NP4Z, N5AW/0
_RST_RE = re.compile(r"^[5E][9N][9N]$")    # 599, 5NN, ENN: contest report before the number
_MERGED_PREFIXES = ("CQ", "TEST", "CWT", "SST", "MST", "DE")
_TRIGGER_WORDS = ("CQ", "TEST", "CWT", "SST", "MST")
_STOP_WORDS = frozenset({"TU", "DE", "CQ", "TEST", "CWT", "SST", "MST", "QRZ", "AGN",
                         "NR", "TNX", "GL", "BK"})


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


def tokens(text: str) -> list[str]:
    """Upper-case word tokens of decoded text, merged keyword prefixes split off."""
    out: list[str] = []
    for t in _SPLIT_RE.split(text.upper()):
        t = t.strip("/")
        if t:
            out.extend(_split_merged(t))
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


def sender(toks: list[str], i: int) -> int | None:
    """Index of the first callsign within LOOKAHEAD tokens after keyword toks[i]."""
    for j in range(i + 1, min(i + 1 + LOOKAHEAD, len(toks))):
        if CALL_RE.match(toks[j]):
            return j
    return None


def is_exchange(toks: list[str], j: int, qualifiers: bool = False) -> bool:
    """toks[j] is followed by NAME then a number or 2-letter state, or by a report (5NN) then
    a number: a caller being sent the exchange. Repeats of the call and 1-character or '?'
    tokens before NAME are skipped. qualifiers (spot_rule_cq_qualifiers): a CQ qualifier of
    the window is no NAME and a stop word is no number or state (CQ PA WB3EML PA CQ)."""
    k = j + 1
    while k < len(toks) and (toks[k] == toks[j] or len(toks[k]) <= 1 or "?" in toks[k]):
        k += 1
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


def runner_calls(text: str, every: bool = False, qualifiers: bool = False) -> list[str]:
    """Senders of CQ groups (rules 1 and 4). A group opens at CQ or TEST, or at DE after a CQ.
    Its sender is the call inside it or right after the keywords (within LOOKAHEAD). Keywords
    right after the sender close the group; the next transmission (usually a caller) starts
    after them: CQ TEST W6YH, CQ W6YH TEST, TEST W6YH, CQ TEST W6YH TEST, and the call sent
    twice (CQ TEST W6YH W6YH TEST). TU W6YH and a bare W6YH TEST do not name W6YH: on the 40m
    CWT 10-08 and 20m CWT 09-23 they promoted callers (+28 and +18 calls CW Skimmer did not
    send, for 3 more runners). The TEST of a bare W6YH TEST still opens a group on the call
    after it; only the exchange test keeps that caller out. every: one entry per group
    instead of one per call."""
    return [call for call, _after in runner_groups(text, every, qualifiers)]


def _call_before(toks: list[str], i: int) -> str | None:
    """The callsign right before toks[i] (1-character and '?' tokens skipped), else None."""
    k = i - 1
    while k >= 0 and (len(toks[k]) <= 1 or "?" in toks[k]):
        k -= 1
    return toks[k] if k >= 0 and CALL_RE.match(toks[k]) else None


def _signing(toks: list[str], k: int, qualifiers: bool = False) -> bool:
    """toks[k] is a call in a sign-off: after TU or DE, or before CQ, TEST or another trigger
    word (1-character and '?' tokens skipped), and not being sent the exchange. A bare repeat
    of the call does not count: a fragment picked once can repeat on its own."""
    if is_exchange(toks, k, qualifiers):
        return False
    p = k - 1
    while p >= 0 and (len(toks[p]) <= 1 or "?" in toks[p]):
        p -= 1
    n = k + 1
    while n < len(toks) and (len(toks[n]) <= 1 or "?" in toks[n]):
        n += 1
    return ((p >= 0 and toks[p] in ("TU", "DE"))
            or (n < len(toks) and toks[n] in _TRIGGER_WORDS))


def runner_groups(text: str, every: bool = False,
                  qualifiers: bool = False) -> list[tuple[str, str | None]]:
    """runner_calls() with, for each call, the callsign right before a bare TEST that opened
    its group (TU W6YH TEST EI4KF -> ("EI4KF", "W6YH")), else None. Lock-on uses it: when W6YH
    owns the frequency, that TEST is W6YH signing and EI4KF is a caller."""
    toks = tokens(text)
    out: list[tuple[str, str | None]] = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        de = t == "DE" and _cq_before(toks, i)
        if not (t in KEYWORDS or de):
            i += 1
            continue
        j = sender(toks, i)
        if j is None or (de and toks.count(toks[j]) < 2):
            i += 1
            continue
        if not is_exchange(toks, j, qualifiers) and (every or all(toks[j] != c for c, _ in out)):
            out.append((toks[j], _call_before(toks, i) if t == "TEST" else None))
        i = j + 1
        while i < n and toks[i] == toks[j]:       # call sent twice before the closing keyword
            i += 1
        while i < n and toks[i] in KEYWORDS:      # closing keywords end the group
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


@dataclass(frozen=True)
class _Copy:
    time: float
    window: int
    call: str
    freq_khz: float
    snr: float
    nth: int = 0              # which CQ group of its window (always 0 without cq_groups)


class RepeatSpotRule:
    """Feed every decode window; returns the spots the window completes."""

    def __init__(self, tier: Callable[[str], int], cq_groups: bool = False,
                 lockon: bool = False, split_runs: bool = False,
                 cq_qualifiers: bool = False) -> None:
        self.tier = tier
        self.cq_groups = cq_groups
        self.lockon = lockon
        self.split_runs = split_runs
        self.cq_qualifiers = cq_qualifiers
        self._owners: list[list] = []                           # lock-on: [call, freq_khz, last seen]
        self._copies: deque[_Copy] = deque()                    # oldest first
        self._seen: set[tuple[int, str, int]] = set()           # (window, call, nth) in _copies
        self._window_ids: dict[tuple[int, int], tuple[int, float]] = {}   # -> (id, first seen)
        self._next_window = 0
        self._last_spot: dict[tuple[str, int], tuple[float, float]] = {}   # (call, MHz) -> (time, freq)
        self._pruned = 0.0

    def window_id(self, bin_id: int, window_key: int, now: float) -> int:
        """Stable id for one bin's decode window (both LPF paths map to the same id)."""
        key = (bin_id, window_key)
        if key not in self._window_ids:
            self._window_ids[key] = (self._next_window, now)
            self._next_window += 1
        return self._window_ids[key][0]

    def feed(self, window: int, freq_khz: float, text: str, now: float,
             snr: float | None = None) -> list[tuple[str, float]]:
        """Record one decoded text of a window; return [(call, freq_khz)] to spot now."""
        self._expire(now)
        if self.split_runs:
            text = split_runs(text)
        spots = []
        calls = (self._lockon_calls(freq_khz, text, now) if self.lockon
                 else runner_calls(text, every=self.cq_groups, qualifiers=self.cq_qualifiers))
        for call, n in Counter(calls).items():
            for nth in range(n):
                if (window, call, nth) not in self._seen:
                    self._seen.add((window, call, nth))
                    self._copies.append(_Copy(now, window, call, freq_khz,
                                              snr if snr is not None else float("-inf"), nth))
            near = [c for c in self._copies if abs(c.freq_khz - freq_khz) <= NEAR_KHZ]
            counts = Counter(c.call for c in near)
            if any(_lev1(other, call) and n >= counts[call] for other, n in counts.items()):
                continue          # a call one edit away has as many copies here: undecided
            if counts[call] < self.tier(call):
                continue
            spot_f = max((c for c in near if c.call == call), key=lambda c: c.snr).freq_khz
            key = (call, int(spot_f // 1000))     # per band: a station running two bands
            last = self._last_spot.get(key)       # (multi-op, SO2R, 2BSIQ) is not re-spotted at each switch
            if last is None or now - last[0] >= REPEAT_S or abs(spot_f - last[1]) >= MOVE_KHZ:
                self._last_spot[key] = (now, spot_f)
                spots.append((call, spot_f))
        return spots

    def owner(self, freq_khz: float, now: float) -> str | None:
        """Lock-on: the call owning the frequency nearest freq_khz (within NEAR_KHZ), else None."""
        live = [o for o in self._owners
                if abs(o[1] - freq_khz) <= NEAR_KHZ and now - o[2] <= LOCK_IDLE_S]
        return min(live, key=lambda o: abs(o[1] - freq_khz))[0] if live else None

    def _own(self, call: str, freq_khz: float, now: float) -> None:
        self._owners = [o for o in self._owners
                        if abs(o[1] - freq_khz) > NEAR_KHZ and now - o[2] <= LOCK_IDLE_S]
        self._owners.append([call, freq_khz, now])

    def _lockon_calls(self, freq_khz: float, text: str, now: float) -> list[str]:
        """Calls one decoded text counts as copies of, with lock-on (see the module docstring)."""
        calls: list[str] = []
        toks = tokens(text)
        for call, after in runner_groups(text, every=self.cq_groups,
                                         qualifiers=self.cq_qualifiers):
            owner = self.owner(freq_khz, now)
            if after is not None and after != call and owner == after:
                continue                          # the owner signing (W6YH TEST): call is a caller
            if owner is not None and call != owner:
                if owner in toks:
                    continue                      # the owner is still here: CQ A6QP K6NR is a garble
                held = sum(1 for c in self._copies if c.call == call
                           and abs(c.freq_khz - freq_khz) <= NEAR_KHZ)
                calls.append(call)                # a challenger copy, in a window without the owner
                if held + 1 >= self.tier(call):
                    self._own(call, freq_khz, now)
                continue
            calls.append(call)
            self._own(call, freq_khz, now)
        owner = self.owner(freq_khz, now)
        if owner is not None and owner not in calls:
            if any(tk == owner and _signing(toks, k, self.cq_qualifiers)
                   for k, tk in enumerate(toks)):
                calls.append(owner)               # TU W6YH, W6YH TEST: the owner signing
                self._own(owner, freq_khz, now)
        return calls

    def _expire(self, now: float) -> None:
        cutoff = now - REPEAT_S
        while self._copies and self._copies[0].time < cutoff:
            c = self._copies.popleft()
            self._seen.discard((c.window, c.call, c.nth))
        if now - self._pruned >= 60.0:
            self._pruned = now
            self._last_spot = {k: v for k, v in self._last_spot.items() if v[0] >= cutoff}
            self._window_ids = {k: v for k, v in self._window_ids.items() if v[1] >= cutoff}
