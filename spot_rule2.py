"""Gate spot rule (spot_rule_gates): a call is spotted only when every gate below is true,
checked in this order; a call not spotted is dropped by the first gate that is false.

  S Buffer             each signal (decoder bin) keeps its last 256 decoded characters, one
                       continuous text, first in first out; the buffer ends with its bin; of
                       a window's two filter paths it keeps the lower-cost copy
  1 Shape              prefix (@, @@, #@ or #@@), 1 to 4 digits, 1 to 6 letters
  2 Not banned         the call is not in the Bad Calls file, and is in
                       MASTER.SCP or on the known-call list when the node's MASTER.SCP filter
                       is on (spot_rule_scp; a beacon need not be)
  3 Reads              reads in the buffer: 2 for a patt3ch.lst + pattern, 3 unflagged, 5 none
                       (normal level), one more for a call not on the node's known-call list
                       (spot_validation_calls); a call decoded twice is read twice
  4 Best reading       the most-read call in the buffer wins; a tie spots neither. A call split by a
                       gap (K1V UT) is a read of the longer call read whole there (K1VUT); a
                       cut-off copy of a longer call read there (N5T of N5TJ) never wins; noise or
                       sign-off letters (E T I S K) never make a longer call (K1TRE, KE8UEX K),
                       nor does a portable form (W1AW/7 is W1AW)
  5 Keyword            the buffer holds CQ or TEST, or an enabled qualifier
  6 Qualifier          a qualifier the node profile enables acts as the event's CQ (SS N6RO)
  7 Tied               the call is the first callsign within 3 words after the keyword (noise
                       letters do not count; a sign-off AR KN SK BK TU ends the CQ), or follows
                       DE with CQ or TEST up to 3 words before it, or ends TU <call> TEST; the
                       call right before DE is being called (CQ S52Y DE N8EM); a read followed
                       by its exchange is a QSO, never tied
  8 Own keyword        the keyword does not come right after another call (K1ABC TEST DL1XYZ)
  9 Strongest          the copy beats every other copy of the call more than 1 kHz away on the
                       same band, in the last 60 s by 6 dB; a tie spots none (a multi-op on two
                       bands spots on both; a node's own call goes in BadCalls.txt)
 10 Re-spot            not spotted on this band in the last 10 minutes, unless moved 1 kHz
 11 Window             the frequency is outside every range in the Notched Freqs file (the
                       decoded window is the band plan's CW segment already)
 12 Beacon             a /B, NCDXF or listed beacon skips 5 to 8; an NCDXF or listed beacon
                       spots on one read (it sends its call once a slot); an NCDXF beacon on its
                       frequency skips 4 too (its fading dashes decode as noise words that tie
                       it), and other such beacons are not a listed beacon's rivals; on an NCDXF
                       frequency a word one letter off one NCDXF call is read as that call
                       (UH6RS is KH6RS), unless it is on the known-call list

A spotted base call goes out in its portable form (VE2PID/W1, TI5/VA3RA) when the decoder read
that form, split at the slash or whole, in 2 windows or more of its buffer (spot_rule.SLASH_MIN).

Every decision is kept in .trace and logged at debug level as (gate, reason)."""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, deque
from collections.abc import Callable, Iterable
from itertools import pairwise
from pathlib import Path

from spot_rule import (
    _NOISE_LETTERS,
    LOOKAHEAD,
    RESPOT_S,
    SLASH_MIN,
    _fragment,
    _lev1,
    is_exchange,
    slash_forms,
    split_runs,
    tokens,
)

log = logging.getLogger(__name__)

KEYWORDS = frozenset({"CQ", "TEST"})   # gate 5; UP would spot a split DX's callers, QRZ a net's (#56 EK6)
QUALIFIERS = frozenset({"SS", "NA", "FD", "CWT", "SST", "MST", "POTA", "SOTA", "WWFF", "IOTA",
                        "SKCC", "QRP", "DX"})                           # gate 6; enabled per node
CALL_RE = re.compile(r"^(?:[A-Z0-9]{1,3}/)?(?:[A-Z]{1,2}|[0-9][A-Z]{1,2})[0-9]{1,4}[A-Z]{1,6}"
                     r"(?:/[A-Z0-9]{1,3})?$")                # a call token, #@@ included
READS = {"active": 2, "rare": 3, None: 5}   # gate 3, by patt3ch.lst pattern
LEVEL_READS = {"permissive": -1, "normal": 0, "aggressive": 1}   # gate 3: call validation level
UNLISTED_READS = 1            # gate 3: more reads for a call not on the known-call list
BEACON_READS = 1              # gate 12: an NCDXF or listed beacon sends its call once a slot
BUFFER_CHARS = 256            # a signal's decoded text judged at once
DE_LOOKBACK = LOOKAHEAD       # gate 7: words searched before DE for the keyword, as after it
SIGNOFF = frozenset({"AR", "KN", "SK", "BK", "TU"})   # gate 7: ends the CQ (not K, not BT)
WINDOW_S = 600.0              # window ids live this long (as spot_rule.py)
PRUNE_S = 60.0                # window ids are pruned this often (as spot_rule.py)
STRUCT_RE = re.compile(r"^(?:[A-Z]{1,2}|[0-9][A-Z]{1,2})[0-9]{1,4}[A-Z]{1,6}$")   # gate 1
MOVE_KHZ = 1.0                # gates 9 and 10: a move this large is another frequency
STRONGER_S = 60.0             # gate 9: a stronger copy this recent
SAME_BAND_KHZ = 600.0         # gate 9: copies this close share a band (CW segments < 300 kHz)
STRONGER_DB = 6.0
BEACON_KHZ = 5.0              # gate 12: a listed beacon within this of its frequency
NCDXF = frozenset({"4U1UN", "VE8AT", "W6WX", "KH6RS", "ZL6B", "VK6RBP", "JA2IGY", "RR9O", "VR2B",
                   "4S7B", "ZS6DN", "5Z4B", "4X6TU", "OH2B", "CS3B", "LU4AA", "OA4B", "YV5B"})
NCDXF_KHZ = (14100.0, 18110.0, 21150.0, 24930.0, 28200.0)
BEACON_MIN_CHARS = 3          # gate 12: shorter words are not read as an NCDXF call

REASONS = {1: "not a callsign structure", 2: "banned call", 3: "not read enough",
           4: "not the best reading", 5: "no keyword", 7: "call not with the keyword",
           8: "the keyword follows another call", 9: "stronger copy elsewhere",
           10: "spotted in the last 10 min", 11: "outside the RBN window or notched"}
SAID = {1: "Not a valid call", 2: "On the bad calls list", 3: "Not copied clearly enough",
        4: "Not copied clearly enough", 5: "Not heard calling CQ", 7: "Not heard calling CQ",
        8: "Not heard calling CQ", 9: "Already spotted", 10: "Already spotted",
        11: "Outside the spotted range"}       # SparkGap Web's words for a gate; the log keeps REASONS


def said(gate: int, why: str) -> str:
    """A decision in SparkGap Web's words: a pass names the keyword or qualifier that passed
    (Spotted CQ, Spotted POTA, Spotted beacon), a fail its group of gates; gate 2 says which list
    stopped the call, so an owner knows what to change."""
    if gate == 0:
        return why.split(",")[0]
    if gate == 2 and why.startswith("not in SCP"):     # gate 2's SCP check, not the bad calls list
        return "Not in SCP"
    return SAID.get(gate, "Not spotted")


def beacon_kind(call: str, khz: float, beacons: dict[str, float] | None = None) -> str | None:
    """How RBN types a spot of this call: "NCDXF" (an NCDXF call within 5 kHz of an NCDXF frequency),
    "listed" (a spot_rule_beacons call near its kHz) or "/B" (any call ending /B); None for a CQ spot.
    The feeder sends it as the spot's type, the archive's mode column shows NCDXF B or BEACON."""
    if call in NCDXF and any(abs(khz - x) <= BEACON_KHZ for x in NCDXF_KHZ):
        return "NCDXF"
    if beacons and call in beacons and abs(khz - beacons[call]) <= BEACON_KHZ:
        return "listed"
    if call.upper().endswith("/B"):
        return "/B"
    return None


def read_bad_calls(path: str | Path) -> list[str]:
    """Bad Calls file (the Aggregator's BadCalls.txt): one call per line."""
    return [ln.strip().upper() for ln in Path(path).read_text().splitlines() if ln.strip()]


def read_notched(path: str | Path) -> list[tuple[float, float]]:
    """Notched Freqs file (the Aggregator's NotchedFreqs.txt): a kHz or a kHz range per line
    (7074, 7047.5-7048.5 or 7047.5 7048.5); a line that is not a number is skipped."""
    out = []
    for ln in Path(path).read_text().splitlines():
        parts = ln.replace("-", " ").replace(",", ".").split()
        try:
            lo = float(parts[0])
            hi = float(parts[1]) if len(parts) > 1 else lo
        except (IndexError, ValueError):
            continue
        out.append((lo, hi))
    return out


def base(call: str) -> str:
    """The longest part of a portable call: HK3/NP4Z/P -> NP4Z."""
    return max(call.split("/"), key=len)


def _near(a: str, b: str) -> bool:
    """b is a near reading of a: one edit, a piece of it, or a with letters glued in front."""
    return _lev1(a, b) or _fragment(b, a) or _fragment(a, b)


def _sender(toks: list[str], i: int) -> int | None:
    """Gate 7: index of the first callsign within LOOKAHEAD words after the keyword at i; a
    noise letter takes no slot, a sign-off ends the CQ (as spot_rule.sender with la1d, but K
    is no sign-off: calls begin and end in K)."""
    seen = 0
    for j in range(i + 1, len(toks)):
        t = toks[j]
        if len(t) == 1 and t in _NOISE_LETTERS:
            continue
        if CALL_RE.match(t):
            return j
        if t in SIGNOFF:
            return None
        seen += 1
        if seen >= LOOKAHEAD:
            return None
    return None


def _keyword_before_de(toks: list[str], i: int, triggers: frozenset[str]) -> str | None:
    """Gate 7: the keyword within DE_LOOKBACK words before DE at i, no callsign between; the call
    right before DE is the station being called (CQ S52Y DE N8EM)."""
    start = i - 1
    if start >= 0 and CALL_RE.match(toks[start]):
        start -= 1
    for k in range(start, max(i - 1 - DE_LOOKBACK, -1), -1):
        if CALL_RE.match(toks[k]):
            return None
        if toks[k] in triggers:
            return toks[k]
    return None


def keyword_calls(toks: list[str],
                  triggers: frozenset[str] = KEYWORDS) -> list[tuple[int, str, bool]]:
    """(token index, keyword, other) of every read gate 7 ties to a keyword or enabled
    qualifier: the first callsign after it, the call after DE, TU CALL TEST, and repeats right
    after a tied read (CQ K1ABC K1ABC). A call right before DE, or followed by its exchange, is
    never tied. other is gate 8: the keyword came right after another call (K1ABC TEST DL1XYZ)."""
    tied: dict[int, tuple[str, bool]] = {}
    for i, t in enumerate(toks):
        if t in triggers:
            j = _sender(toks, i)
            if j is not None:
                other = i >= 1 and toks[i - 1] != toks[j] and CALL_RE.match(toks[i - 1]) is not None
                tied.setdefault(j, (t, other))
        elif t == "DE" and i + 1 < len(toks) and CALL_RE.match(toks[i + 1]):
            kw = _keyword_before_de(toks, i, triggers)
            if kw:
                tied.setdefault(i + 1, (kw, False))
        elif (t == "TU" and i + 2 < len(toks) and CALL_RE.match(toks[i + 1])
              and toks[i + 2] == "TEST"):
            tied.setdefault(i + 1, ("TEST", False))
    for j in sorted(tied):
        k = j + 1
        while k < len(toks) and toks[k] == toks[j]:
            tied.setdefault(k, tied[j])
            k += 1
    return [(j, kw, other) for j, (kw, other) in sorted(tied.items())
            if not (j + 1 < len(toks) and toks[j + 1] == "DE")
            and not is_exchange(toks, j, qualifiers=True)]


def reads(call: str, buf: list[str]) -> int:
    """Gates 3 and 4: how often the call is read in the buffer."""
    return buf.count(call)


TRACE_KEPT = 10_000   # the newest gate decisions kept in .trace
CLIP_EXTRA_NOISE = frozenset("ETISK")   # gate 4: K1TRE is K1TR and a noise letter


def best_reads(buf: list[str]) -> Counter[str]:
    """Gate 4: the reads of each call, a call split by a gap (K1V UT) counted for the longer
    call when that call is read whole in the same buffer (K1VUT)."""
    n = Counter(t for t in buf if CALL_RE.match(t))
    for a, b in pairwise(buf):
        if n[a] and b.isalpha() and len(b) <= 3 and not set(b) <= CLIP_EXTRA_NOISE and n[a + b]:
            n[a] -= 1
            n[a + b] += 1
    return n


def clip_of(call: str, longer: str) -> bool:
    """Gate 4: call is a cut-off copy of longer (N5T of N5TJ, C6X of NC6X): its start or end,
    with more than noise or sign-off letters missing."""
    if longer == call or "/" in longer:   # W1AW/7 is W1AW portable, the same station
        return False
    extra = (longer[len(call):] if longer.startswith(call)
             else longer[:-len(call)] if longer.endswith(call) else "")
    return bool(extra) and not set(extra) <= CLIP_EXTRA_NOISE   # glued noise: SW0YB, K1TRE


class GateSpotRule:
    """Drop-in for spot_rule.RepeatSpotRule (window_id, feed, spot_snr, last_spots,
    restore_last_spots), built from the gates in this module's docstring."""

    split_runs = de_rejoin = runner_signs = window_join = False   # sparkgap's start-up log

    def __init__(self, shape: Callable[[str], str | None],
                 listed: Callable[[str], bool] | None = None,
                 banned: Iterable[str] = (),
                 beacons: dict[str, float] | None = None, stronger_db: float | None = STRONGER_DB,
                 best_reading: bool = True, qualifiers: Iterable[str] = (),
                 notched: Iterable[tuple[float, float]] = (), level: str = "normal",
                 scp: Callable[[str], bool] | None = None):
        if level not in LEVEL_READS:
            raise ValueError(f"call validation level {level!r}: not one of {list(LEVEL_READS)}")
        self.shape = shape                  # patt3ch.lst: "active" (+), "rare" or None
        self.level = level                  # gate 3: spot_validation
        self.listed = listed                # gate 3: the known-call list, or None
        self.banned = frozenset(banned)
        self.scp = scp                      # gate 2, the MASTER.SCP filter: in MASTER.SCP, or None
        self.notched = [tuple(n) for n in notched]
        self.beacons = dict(beacons or {})
        self.stronger_db = stronger_db
        self.best_reading = best_reading
        self.triggers = KEYWORDS | (frozenset(qualifiers) & QUALIFIERS)    # gates 5 and 6
        self.spot_snr: dict[str, float] = {}
        self.spot_why: dict[tuple[str, float], str] = {}   # (call, kHz) of a spot -> why it passed, until taken
        # (t, kHz, call, gate, why)
        self.trace: deque[tuple[float, float, str, int, str]] = deque(maxlen=TRACE_KEPT)
        # bin -> {window: text}, oldest first
        self._bins: dict[int, dict[int, str]] = {}
        self._win_bin: dict[int, int] = {}
        self._reads: deque[tuple[float, str, float, float, int]] = deque()  # (t, call, kHz, SNR, window): gate 9
        self._last_spot: dict[tuple[str, int], tuple[float, float]] = {}
        # gate 9: calls that passed every gate this pass, spotted by end_pass()
        self._held: list[tuple[float, str, float, float | None, float | None, str, int]] = []
        self._forms: dict[int, set[str]] = {}     # window -> portable calls read in it, see _slash_form()
        self._cost: dict[int, float] = {}         # gate S: window -> the decode cost of the copy it keeps
        self._window_ids: dict[tuple[int, int], tuple[int, float]] = {}   # -> (id, first seen)
        self._pruned = 0.0
        self._next_window = 0

    def window_id(self, bin_id: int, window_key: int, now: float) -> int:
        key = (bin_id, window_key)
        if key not in self._window_ids:
            self._window_ids[key] = (self._next_window, now)
            self._win_bin[self._next_window] = bin_id
            self._next_window += 1
        return self._window_ids[key][0]

    def _decide(self, now: float, f: float, call: str, req: int, why: str = "") -> bool:
        why = why or REASONS.get(req, "")
        self.trace.append((now, f, call, req, why))
        msg = f"gate {req} false: {why}" if req else why
        log.debug("spot rule %s %.1f: %s", call, f, msg)
        return req == 0

    def _expire(self, now: float) -> None:
        while self._reads and now - self._reads[0][0] > STRONGER_S:
            self._reads.popleft()
        if now - self._pruned >= PRUNE_S:
            self._pruned = now
            self._last_spot = {k: v for k, v in self._last_spot.items() if now - v[0] < RESPOT_S}
            self._window_ids = {k: v for k, v in self._window_ids.items() if now - v[1] <= WINDOW_S}
            live = {v[0] for v in self._window_ids.values()}
            self._win_bin = {w: b for w, b in self._win_bin.items() if w in live}
            self._forms = {w: f for w, f in self._forms.items() if w in live}
            self._cost = {w: c for w, c in self._cost.items() if w in live}

    def close_bin(self, bin_id: int) -> None:
        """Gate S: the buffer ends with its bin."""
        self._bins.pop(bin_id, None)

    @staticmethod
    def _trim(texts: dict[int, str]) -> None:
        """Gate S: drop windows wholly older than the last BUFFER_CHARS characters before the
        newest window, which its other filter path's copy may still replace with a shorter text."""
        size = 0
        old = list(texts)
        for i in range(len(old) - 2, -1, -1):
            if size >= BUFFER_CHARS:
                for w in old[: i + 1]:
                    del texts[w]
                return
            size += len(texts[old[i]]) + 1

    def buffer(self, window: int) -> list[str]:
        """Gate S: the tokens of the last BUFFER_CHARS characters of this window's signal, one
        continuous text across windows."""
        texts = self._bins.get(self._win_bin.get(window, -1), {}).values()
        words = " ".join(texts).split()
        toks: list[str] = []
        size = 0
        while words and size + len(words[-1]) + 1 <= BUFFER_CHARS:
            size += len(words[-1]) + 1
            toks.append(words.pop())
        return toks[::-1]

    def beacon(self, call: str, f: float) -> bool:
        return call.endswith("/B") or self.known_beacon(call, f)

    def _ncdxf(self, tok: str) -> str:
        """Gate 12: the NCDXF call a word on an NCDXF frequency is one letter off, if only one."""
        if (tok in NCDXF or len(tok) < BEACON_MIN_CHARS
                or (self.listed is not None and self.listed(base(tok)))):
            return tok
        near = [c for c in NCDXF if _lev1(tok, c)]
        return near[0] if len(near) == 1 else tok

    def known_beacon(self, call: str, f: float) -> bool:
        if call in NCDXF:
            return any(abs(f - x) <= BEACON_KHZ for x in NCDXF_KHZ)
        return call in self.beacons and abs(f - self.beacons[call]) <= BEACON_KHZ

    def feed(self, window: int, freq_khz: float, text: str, now: float,
             snr: float | None = None, report: float | None = None,
             cost: float | None = None) -> list[tuple[str, float]]:
        """One decoded text of a window. A call that passes every gate is held, not returned:
        end_pass() spots it once every window decoded in the same pass is in (gate 9 then sees a
        stronger copy in another bin whatever order the bins were fed in). A window decoded
        more than once (the two filter paths) keeps its lower-cost copy (the decoder's cost; the
        latest when either has none): a cleaner copy replaces what the other one held and read."""
        self._expire(now)
        cost = cost if cost is not None and math.isfinite(cost) else None
        kept = self._cost.get(window)
        if cost is not None and kept is not None:
            if cost >= kept:
                return []                             # gate S: the window keeps its cleaner copy
            self._held = [h for h in self._held if h[6] != window]
            self._reads = deque(r for r in self._reads if r[4] != window)
        if cost is None:
            self._cost.pop(window, None)
        else:
            self._cost[window] = cost
        forms = slash_forms(text)                     # VE2PID /W1: tokens() below splits it at the slash
        if forms:
            self._forms[window] = forms
        else:
            self._forms.pop(window, None)             # a window decoded again keeps the copy kept
        text = " ".join(tokens(split_runs(text, de_rejoin=True)))     # CQCQDE -> CQ CQ DE
        if any(abs(freq_khz - x) <= BEACON_KHZ for x in NCDXF_KHZ):
            text = " ".join(self._ncdxf(t) for t in text.split())
        b = self._win_bin.setdefault(window, window)
        texts = self._bins.setdefault(b, {})
        texts.pop(window, None)
        texts[window] = text                          # newest last
        self._trim(texts)
        buf = self.buffer(window)
        tied = keyword_calls(buf, self.triggers)
        if report is not None:
            snr = report                  # gate 9 ranks copies by the reported SNR
        for call in dict.fromkeys(t for t in text.split() if CALL_RE.match(t)):
            if snr is not None:
                self._reads.append((now, call, freq_khz, snr, window))
            if self._check(call, buf, tied, freq_khz, now, snr):
                self._held.append((now, call, freq_khz, snr, report, self.trace[-1][4], window))
        return []

    def _stronger(self, call: str, f: float, snr: float | None) -> bool:
        """Gate 9: another copy of the call more than 1 kHz away on the same band, that this copy
        does not beat by 6 dB: a stronger copy, or a tie (one station is not on two frequencies of
        a band)."""
        return (self.stronger_db is not None and snr is not None
                and any(c == call and MOVE_KHZ < abs(rf - f) < SAME_BAND_KHZ
                        and rs > snr - self.stronger_db for _, c, rf, rs, _ in self._reads))

    def latest(self, call: str, f: float) -> tuple[int, str] | None:
        """The newest decision kept on this call within 1 kHz of f: (gate, why), gate 0 a pass."""
        for _, rf, c, req, why in reversed(list(self.trace)):    # a copy: the feed thread asks while decoding appends
            if c == call and abs(rf - f) < MOVE_KHZ:
                return req, why
        return None

    def end_pass(self) -> list[tuple[str, float]]:
        """Spot the calls held this pass: gates 9 and 10 again, now that every window of the
        pass is in. Returns [(call, kHz)]."""
        spots: list[tuple[str, float]] = []
        held, self._held = self._held, []
        for now, call, f, snr, report, why, window in held:
            if self._stronger(call, f, snr):
                self._decide(now, f, call, 9)
                continue
            last = self._last_spot.get((call, int(f // 1000)))
            if last and now - last[0] < RESPOT_S and abs(f - last[1]) < MOVE_KHZ:
                continue                              # the other filter path of a call just spotted
            out = self._slash_form(call, window)       # VE2PID read as VE2PID/W1 here too: spot VE2PID/W1
            spots.append((out, f))
            self._last_spot[(call, int(f // 1000))] = (now, f)
            self.spot_why[(out, f)] = said(0, why)             # flush_pass takes it under the call sent
            if report is not None or snr is not None:
                self.spot_snr[out] = report if report is not None else snr      # type: ignore[assignment]
        return spots

    def _check(self, call: str, buf: list[str], tied: list[tuple[int, str, bool]],
               f: float, now: float, snr: float | None) -> bool:
        b = base(call)
        if not STRUCT_RE.match(b):
            return self._decide(now, f, call, 1)
        if call in self.banned or b in self.banned:
            return self._decide(now, f, call, 2)
        beacon = self.beacon(call, f)
        if (self.scp is not None and not beacon and not (self.scp(call) or self.scp(b))
                and (self.listed is None or not self.listed(b))):     # a known call needs no SCP entry
            return self._decide(now, f, call, 2, "not in SCP" if self.listed is None else "not in SCP or the known calls")
        known = self.known_beacon(call, f)
        kind = self.shape(b)
        unlisted = self.listed is not None and not self.listed(b)
        need = (BEACON_READS if known
                else READS[kind] + LEVEL_READS[self.level] + (UNLISTED_READS if unlisted else 0))
        n = reads(call, buf)
        if n < need:
            why = f"{kind or 'no'} pattern, {self.level}{', not listed' if unlisted else ''}"
            return self._decide(now, f, call, 3, f"{n} of {need} reads ({why})")
        if self.best_reading and not (known and call in NCDXF):   # gate 12: an NCDXF beacon has no rival
            counts = best_reads(buf)
            longer = next((t for t in counts if counts[t] > 0 and clip_of(call, t)), None)
            if longer:
                return self._decide(now, f, call, 4, f"a cut-off copy of {longer}")
            mine = counts[call]
            # neither W1AW/7 nor a cut-off copy (C6X of NC6X) is a rival
            rival = max(((c, t) for t, c in counts.items()
                         if base(t) != base(call) and not clip_of(t, call)
                         and not (known and self.known_beacon(t, f))), default=(0, ""))
            if rival[0] >= mine:
                return self._decide(now, f, call, 4, f"{rival[1]} read {rival[0]}x, this {mine}x")
        kw = None
        if not beacon:
            mine = [(k, other) for i, k, other in tied if buf[i] == call]
            kw = next((k for k, other in mine if not other), None)
            if kw is None and mine:
                return self._decide(now, f, call, 8)
            if kw is None and not any(t in self.triggers for t in buf):
                return self._decide(now, f, call, 5)
            if kw is None:
                return self._decide(now, f, call, 7)
        if self._stronger(call, f, snr):
            return self._decide(now, f, call, 9)
        last = self._last_spot.get((call, int(f // 1000)))
        if last and now - last[0] < RESPOT_S and abs(f - last[1]) < MOVE_KHZ:
            return self._decide(now, f, call, 10)
        if any(lo <= f <= hi for lo, hi in self.notched):
            return self._decide(now, f, call, 11, "notched frequency")
        return self._decide(now, f, call, 0, f"Spotted {'beacon' if beacon else kw}, {n} reads")

    def _slash_form(self, call: str, window: int) -> str:
        """The call as it goes out: a base call whose portable form (VE2PID/W1, TI5/VA3RA) was read
        in SLASH_MIN windows or more of its signal's buffer goes out in that form, as other skimmers
        spot it (spot_rule.RepeatSpotRule._slash_form). The gates judge the base call, read as
        tokens() reads it; one read of a form is not enough, a suffix garbled once would send a
        wrong call."""
        if "/" in call:
            return call
        wins: dict[str, int] = {}
        for w in self._bins.get(self._win_bin.get(window, -1), {}):
            for form in self._forms.get(w, ()):
                if call in form.split("/"):
                    wins[form] = wins.get(form, 0) + 1
        best = max(wins.items(), key=lambda kv: kv[1], default=(call, 0))
        return best[0] if best[1] >= SLASH_MIN else call

    def last_spots(self) -> list[list]:
        return [[c, mhz, t, f] for (c, mhz), (t, f) in self._last_spot.items()]

    def restore_last_spots(self, rows: list, now: float) -> int:
        kept = 0
        for row in rows:
            try:
                call, mhz, t, f = str(row[0]), int(row[1]), float(row[2]), float(row[3])
            except (TypeError, ValueError, IndexError):
                continue
            if 0 <= now - t < RESPOT_S:
                self._last_spot[(call, mhz)] = (t, f)
                kept += 1
        return kept
