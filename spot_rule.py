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

The spot goes out on the strongest window (bin SNR) among the copies, once per call per
REPEAT_S unless it moves more than MOVE_KHZ. A window is one decode window of one bin;
both LPF paths of a window count once.
"""

import re
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass

KEYWORDS = frozenset({"CQ", "TEST"})
LOOKAHEAD = 3                 # tokens after a keyword searched for the sender's call
DE_LOOKBACK = 6               # tokens before DE searched for CQ or TEST
NEAR_KHZ = 0.5                # copies within this of each other are one frequency
REPEAT_S = 600.0              # copies count for 10 minutes; one spot per call per 10 minutes
MOVE_KHZ = 1.0                # ...unless it moves more than this (CW Skimmer re-spots 1 kHz moves)

CALL_RE = re.compile(r"^(?:[A-Z0-9]{1,3}/)?"                      # HK3/
                     r"(?:[A-Z]{1,2}|[0-9][A-Z])[0-9]{1,4}[A-Z]{1,6}"
                     r"(?:/[A-Z0-9]{1,3})?$")                          # /0, /P
_SPLIT_RE = re.compile(r"[^A-Z0-9?/]+")   # keep / for HK3/NP4Z, N5AW/0
_RST_RE = re.compile(r"^[5E][9N][9N]$")    # 599, 5NN, ENN: contest report before the number
_MERGED_PREFIXES = ("CQ", "TEST", "CWT", "SST", "MST", "DE")
_TRIGGER_WORDS = ("CQ", "TEST", "CWT", "SST", "MST")
_STOP_WORDS = frozenset({"TU", "DE", "CQ", "TEST", "CWT", "SST", "MST", "QRZ", "AGN",
                         "NR", "TNX", "GL", "BK"})


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


def _is_trigger_like(t: str) -> bool:
    return t in _TRIGGER_WORDS or any(_one_sub_same_kind(t, w) for w in ("CWT", "TEST"))


def sender(toks: list[str], i: int) -> int | None:
    """Index of the first callsign within LOOKAHEAD tokens after keyword toks[i]."""
    for j in range(i + 1, min(i + 1 + LOOKAHEAD, len(toks))):
        if CALL_RE.match(toks[j]):
            return j
    return None


def is_exchange(toks: list[str], j: int) -> bool:
    """toks[j] is followed by NAME then a number or 2-letter state, or by a report (5NN) then
    a number: a caller being sent the exchange. Repeats of the call and 1-character or '?'
    tokens before NAME are skipped."""
    k = j + 1
    while k < len(toks) and (toks[k] == toks[j] or len(toks[k]) <= 1 or "?" in toks[k]):
        k += 1
    if k + 1 >= len(toks):
        return False
    name, num = toks[k], toks[k + 1]
    if _RST_RE.match(name):                      # CQ WW, WPX: report then zone or serial
        return any(ch.isdigit() for ch in num)
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


def runner_calls(text: str) -> list[str]:
    """Senders of CQ groups (rules 1 and 4). A group opens at CQ or TEST, or at DE after a CQ.
    Its sender is the call inside it or right after the keywords (within LOOKAHEAD). Keywords
    right after the sender close the group; the next transmission (usually a caller) starts
    after them: CQ TEST W6YH, CQ W6YH TEST, TEST W6YH, CQ TEST W6YH TEST, and the call sent
    twice (CQ TEST W6YH W6YH TEST). TU W6YH and a bare W6YH TEST do not name W6YH: on the 40m
    CWT 10-08 and 20m CWT 09-23 they promoted callers (+28 and +18 calls CW Skimmer did not
    send, for 3 more runners). The TEST of a bare W6YH TEST still opens a group on the call
    after it; only the exchange test keeps that caller out."""
    toks = tokens(text)
    out: list[str] = []
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
        if not is_exchange(toks, j) and toks[j] not in out:
            out.append(toks[j])
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


class RepeatSpotRule:
    """Feed every decode window; returns the spots the window completes."""

    def __init__(self, tier: Callable[[str], int]) -> None:
        self.tier = tier
        self._copies: deque[_Copy] = deque()                    # oldest first
        self._seen: set[tuple[int, str]] = set()                # (window, call) in _copies
        self._window_ids: dict[tuple[int, int], tuple[int, float]] = {}   # -> (id, first seen)
        self._next_window = 0
        self._last_spot: dict[str, tuple[float, float]] = {}    # call -> (time, freq)
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
        spots = []
        for call in runner_calls(text):
            if (window, call) not in self._seen:
                self._seen.add((window, call))
                self._copies.append(_Copy(now, window, call, freq_khz,
                                          snr if snr is not None else float("-inf")))
            near = [c for c in self._copies if abs(c.freq_khz - freq_khz) <= NEAR_KHZ]
            counts = Counter(c.call for c in near)
            if any(_lev1(other, call) and n >= counts[call] for other, n in counts.items()):
                continue          # a call one edit away has as many copies here: undecided
            if counts[call] < self.tier(call):
                continue
            spot_f = max((c for c in near if c.call == call), key=lambda c: c.snr).freq_khz
            last = self._last_spot.get(call)
            if last is None or now - last[0] >= REPEAT_S or abs(spot_f - last[1]) >= MOVE_KHZ:
                self._last_spot[call] = (now, spot_f)
                spots.append((call, spot_f))
        return spots

    def _expire(self, now: float) -> None:
        cutoff = now - REPEAT_S
        while self._copies and self._copies[0].time < cutoff:
            c = self._copies.popleft()
            self._seen.discard((c.window, c.call))
        if now - self._pruned >= 60.0:
            self._pruned = now
            self._last_spot = {k: v for k, v in self._last_spot.items() if v[0] >= cutoff}
            self._window_ids = {k: v for k, v in self._window_ids.items() if v[1] >= cutoff}
