"""Voice (SSB) runner spots from Whisper transcripts: the CW repeat rule, spoken.

usage: voice_spot.py DUMPDIR MODEL [--debug]
Reads DUMPDIR/ssb_<carrier kHz>.<MODEL>.json ([[start, end, text, logprob], ...]) and prints
spots: a call heard as the runner (after CQ / QRZ / "this is", or before "QRZ" / "calling CQ" /
"standing by") at least twice, never a caller ("did I copy X", "X you're 5-9", "thank you X").
"""
import glob
import json
import os
import re
import sys

NATO = {
    'alpha': 'A', 'alfa': 'A', 'bravo': 'B', 'charlie': 'C', 'delta': 'D', 'echo': 'E',
    'foxtrot': 'F', 'golf': 'G', 'hotel': 'H', 'india': 'I', 'juliet': 'J', 'juliett': 'J',
    'kilo': 'K', 'lima': 'L', 'mike': 'M', 'november': 'N', 'oscar': 'O', 'papa': 'P',
    'quebec': 'Q', 'romeo': 'R', 'sierra': 'S', 'tango': 'T', 'uniform': 'U', 'victor': 'V',
    'whiskey': 'W', 'whisky': 'W', 'xray': 'X', 'yankee': 'Y', 'zulu': 'Z',
}
# Alternates hams use on the air; only trusted inside a run that already looks like a call.
ALT = {
    'america': 'A', 'amsterdam': 'A', 'boston': 'B', 'baltimore': 'B', 'brazil': 'B',
    'canada': 'C', 'chile': 'C', 'denmark': 'D', 'england': 'E', 'florida': 'F', 'france': 'F',
    'germany': 'G', 'guatemala': 'G', 'honolulu': 'H', 'honduras': 'H', 'italy': 'I',
    'japan': 'J', 'kilowatt': 'K', 'kentucky': 'K', 'london': 'L', 'lima': 'L', 'mexico': 'M',
    'norway': 'N', 'nicaragua': 'N', 'ontario': 'O', 'portugal': 'P', 'peru': 'P',
    'queen': 'Q', 'quebec': 'Q', 'radio': 'R', 'santiago': 'S', 'sugar': 'S', 'spain': 'S',
    'texas': 'T', 'tokyo': 'T', 'united': 'U', 'uruguay': 'U', 'victoria': 'V',
    'venezuela': 'V', 'washington': 'W', 'yokohama': 'Y', 'zanzibar': 'Z',
}
DIGITS = {'zero': '0', 'oh': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
          'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'niner': '9'}
CALL_RE = re.compile(r'^(?:[A-Z]{1,2}|[0-9][A-Z]|[A-Z][0-9])[0-9]{1,2}[A-Z]{1,4}$')
CQ_WORDS = {'cq', 'cqcq'}
RUNNER_AFTER = {'qrz', 'cq', 'contest'}       # plus the pairs "calling cq", "standing by" below
CALLER_BEFORE = {'copy', 'copied', 'thank', 'thanks', 'you', 'your', "you're", 'youre', 'roger', 'worked', 'got'}
CALLER_AFTER = {"you're", 'youre', 'your', 'you', 'five', 'fifty', '59', '57', '58', 'report', 'thanks'}


SUFFIX_WORDS = {'portable', 'stroke', 'slash'}
# Words that may sit between a CQ and the call: "CQ POTA", "CQ DX", "CQ contest", "CQ Parks on the air",
# "CQ 20 meters", "CQ Bigfoot", "calling CQ". Phonetics are never qualifiers.
CQ_QUALIFIERS = {'pota', 'sota', 'wwff', 'dx', 'contest', 'test', 'parks', 'park', 'on', 'the', 'air',
                 'calling', 'cq', 'from', 'meters', 'meter', 'twenty', 'forty', 'fifteen', 'ten', 'eighty',
                 '20', '40', '15', '10', '80', '17', '12', 'bigfoot', 'qso', 'party', 'field', 'day', 'and',
                 'north', 'south', 'america', 'europe', 'asia', 'japan', 'only', 'stateside', 'any', 'station'}
SELF_ID = (['this', 'is'], ['here', 'is'], ["i'm"], ['i', 'am'], ['de'], ['from'])


def after_cq(toks, a):
    """True when a CQ comes before token a with only qualifier words between (at most 6)."""
    k = a - 1
    while k >= 0 and a - k <= 7:
        if toks[k] in CQ_WORDS:
            return True
        if toks[k] not in CQ_QUALIFIERS or unit(toks[k]) is not None and toks[k] not in ('20', '40', '15', '10', '80', '17', '12'):
            return False
        k -= 1
    return False


def self_id(toks, a):
    return any(toks[max(0, a - len(p)):a] == p for p in SELF_ID)


def nato_fragment(w):
    """'fox' for Foxtrot: the start of a phonetic but not the word, so the call may go on."""
    return len(w) >= 3 and w not in NATO and any(n.startswith(w) for n in NATO)


def tokens(text):
    t = text.lower().replace('x-ray', 'xray').replace('x ray', 'xray').replace('seek you', 'cq')
    t = re.sub(r'\bc\s*q\b', 'cq', t)
    t = re.sub(r'\bq\s*r\s*z\b', 'qrz', t)
    t = re.sub(r"[^a-z0-9/' ]+", ' ', t)
    return t.split()


def unit(tok):
    """A token's contribution to a call: (chars, kind) or None. kind: nato/alt/digit/raw."""
    if tok in NATO:
        return NATO[tok], 'nato'
    if tok in DIGITS:
        return DIGITS[tok], 'digit'
    if tok in ALT:
        return ALT[tok], 'alt'
    if re.fullmatch(r'[0-9]', tok):
        return tok, 'digit'
    if re.fullmatch(r'[a-z]', tok):
        return tok.upper(), 'raw'
    if re.fullmatch(r'[a-z0-9]{3,7}', tok) and re.search(r'[0-9]', tok):
        return tok.upper(), 'raw'
    return None


# "Kilo Foxtrot for India": Whisper spells "four" as "for" between phonetics (Fred 10-21: OK)
FOR_FOUR = {'for'}
# "Whiskey Fox 8 Zulu": many say Fox for Foxtrot (Fred 10-22: try it). F only right after a phonetic
# or a digit inside a call, never on its own.
SHORT_PHONETICS = {'fox': 'F'}
# Short words that may follow a call; any other 2-3 letter stub right after one ("KF4I, CE" for
# "K F 4 I zee E") means the call went on and was misheard: the copy is dropped, not repaired.
STUB_OK = {'is', 'in', 'on', 'at', 'to', 'of', 'it', 'me', 'my', 'we', 'he', 'so', 'go', 'do', 'no',
           'ok', 'or', 'an', 'as', 'be', 'by', 'up', 'us', 'oh', 'hi', 'am', 'and', 'the', 'for', 'you',
           'are', 'cq', 'de', 'qrz', 'qsl', 'qrp', 'dx', 'pa', 'qso', 'ssb', 'net', 'now', 'out', 'yes',
           'see', 'all', 'one', 'two', 'six', 'ten', 'can', 'get', 'got', 'has', 'had', 'his', 'her',
           'its', 'not', 'but', 'how', 'who', 'why', 'did', 'way', 'new', 'old', 'big', 'our', 'off',
           'any', 'too', 'say', 'hey', 'yep', 'nah', 'mhz', 'khz', 'kw', 'qth', 'sri', 'tnx', 'tu',
           'ur', 'gm', 'ge', 'ga', 'om', 'yl', 'xyl', 'pota', 'sota', 'iota', 'ah', 'uh', 'um', 'hm',
           'mm', 'eh', 'over', 'from', 'here', 'this', 'that', 'with', 'also', 'just'}


# "Papa 40 Whiskey", "Papa Forty Whiskey" (P40W, 10-24): Whisper writes a call's digit pair as one number.
# Read as two digits only between call letters, so reports and serials ("599", "number 40") stay out.
TENS = {'twenty': '20', 'thirty': '30', 'forty': '40', 'fourty': '40', 'fifty': '50', 'sixty': '60',
        'seventy': '70', 'eighty': '80', 'ninety': '90'}


def units(toks):
    """unit() of each token; "for" is 4 between two phonetic words; a digit pair (40, forty) between
    call letters is two digits."""
    u = [unit(t) for t in toks]
    letter = lambda v: v is not None and v[1] in ('nato', 'alt', 'raw') and v[0].isalpha()   # noqa: E731
    for k in range(1, len(toks) - 1):
        pair = toks[k] if re.fullmatch(r'[0-9]{2}', toks[k]) else TENS.get(toks[k])
        if pair and u[k] is None and letter(u[k - 1]) and letter(u[k + 1]):
            u[k] = (pair, 'digit')
    for k in range(1, len(toks)):
        if toks[k] in SHORT_PHONETICS and u[k] is None and u[k - 1] and u[k - 1][1] in ('nato', 'digit'):
            u[k] = (SHORT_PHONETICS[toks[k]], 'nato')
    for k in range(1, len(toks) - 1):
        if toks[k] in FOR_FOUR and u[k - 1] and u[k + 1] and u[k - 1][1] == 'nato' and u[k + 1][1] == 'nato':
            u[k] = ('4', 'digit')
    return u


def stub(w):
    return w.isalpha() and 2 <= len(w) <= 3 and w not in STUB_OK


def calls_in(toks):
    """[(call, first token index, last index)] for each call-shaped run of phonetic/digit tokens."""
    out, i = [], 0
    u = units(toks)
    while i < len(toks):
        if u[i] is None:
            i += 1
            continue
        j = i
        while j < len(toks) and u[j] is not None:
            j += 1
        end = j                                           # where the next run search starts
        if j - i > 1 and j < len(toks) and stub(toks[j]) and u[j - 1][1] == 'raw':
            j -= 1                                        # "..., KF4I, CE": leave the cut-off letters out
        chars, kinds = '', []
        for k in range(i, j):
            chars += u[k][0]
            kinds.append(u[k][1])
        # char -> token index, so a call found in the run maps back to its tokens
        owner = []
        for n, k in enumerate(range(i, j)):
            owner += [k] * len(u[k][0])
        anchored = (i >= 2 and toks[i - 2:i] == ['this', 'is']) or (i >= 1 and toks[i - 1] in CQ_WORDS | {'de'})
        a = 0
        def ok(x, y):
            ks = {kinds[owner[z] - i] for z in range(x, y)}
            return CALL_RE.match(chars[x:y]) and ('nato' in ks or 'raw' in ks or anchored)

        memo = {}

        def best(x):
            """(score, [(start, end)]) for chars[x:]: cover the most with calls, repeats score extra."""
            if x >= len(chars):
                return 0.0, []
            if x in memo:
                return memo[x]
            res = best(x + 1)                                     # skip a character
            for y in range(x + 3, min(len(chars), x + 7) + 1):
                if not ok(x, y):
                    continue
                tail_s, tail = best(y)
                score = (y - x) - 1 + tail_s
                if tail:                                          # "AA3B AA3B", or "AA3B AA3K" misheard
                    nx, ny = tail[0]
                    if ny - nx == y - x and chars[nx:ny - 1] == chars[x:y - 1]:
                        score += 2
                if score > res[0]:
                    res = (score, [(x, y)] + tail)
            memo[x] = res
            return res

        prev = None
        nxt = toks[j:j + 2]
        if nxt and nxt[0] in SUFFIX_WORDS:              # "portable zero", "stroke 6": a suffix, not more call
            nxt = toks[j + 2:j + 4]
        interrupted = any((unit(w) is not None and unit(w)[1] in ('nato', 'digit')) or nato_fragment(w)
                          for w in nxt)                  # "...Bravo Ocean X-ray", "...Hotel Fox Drive"
        stub_cut = bool(nxt and stub(nxt[0]))           # "KF4I, CE": "zee" misheard, the call went on
        for x, y in ([] if interrupted else best(0)[1]):
            call, last = chars[x:y], owner[y - 1]
            if stub_cut and last == j - 1:
                continue                                  # only the call the stub follows
            if prev and len(prev) == len(call) and prev[:-1] == call[:-1]:
                call = prev                                       # a misheard repeat counts as the first copy
            prev = call
            if last + 2 < len(toks) and toks[last + 1] in SUFFIX_WORDS and toks[last + 2] in DIGITS:
                call += '/' + DIGITS[toks[last + 2]]
            out.append((call, owner[x], last))
        i = end
    return out


STRICT = '--strict' in sys.argv          # precision first: a CQ in the last 30 s for every copy
MIN_LOGPROB = -0.8                       # --strict: skip segments Whisper itself doubts
WORD_MIN = 0.5                           # --strict: every word of a call (and the next) heard this surely
# A spoken digit's confidence is split between the ways Whisper can write it (Seven, seven, 7), so it reads
# low even when clean: "CQ Bigfoot, this is Whiskey, Seven, Oscar, QRZ" scored Seven 0.14-0.19 three times
# (10-22). Digit words are left out of the WORD_MIN check (VS_DIGITS_EXEMPT=0 to restore); phonetics and
# letters still must pass.
import os as _os
DIGITS_EXEMPT = _os.environ.get('VS_DIGITS_EXEMPT', '1') != '0'


def unsure(toks, probs, a, b):
    """The call toks[a..b] (and the word after it) has a word Whisper was unsure of."""
    return any(p < WORD_MIN for k, p in zip(range(a, b + 2), probs[a:b + 2])
               if not (DIGITS_EXEMPT and k < len(toks) and (toks[k] in DIGITS or toks[k].isdigit())))


def word_probs(text, words):
    """Probability per token of tokens(text), from Whisper's words; None if they don't line up."""
    if not words:
        return None
    probs = []
    for w, p in words:
        probs += [p] * len(tokens(w))
    return probs if len(probs) == len(tokens(text)) else None


def worked_calls(segs):
    """Calls given a report on this clip ("K1ABC, you're 59", "QSL K1ABC 5 9"): the runner's QSOs."""
    out = set()
    for seg in segs:
        toks = tokens(seg[2])
        for call, a, b in calls_in(toks):
            nxt = toks[b + 1:b + 4]
            if set(nxt[:2]) & CALLER_AFTER or 'qsl' in toks[max(0, a - 2):a]:
                out.add(call.split('/')[0])
    return out


RUN_FREQ_MIN = 3                              # --runfreq: reports to this many calls = a run frequency


def runner_hits(segs):
    """[(time, call, why)] for each call heard in runner position; callers are left out."""
    hits = []
    window = []                       # (time, tokens) of the last ~30 s
    asked = False
    for seg in segs:
        start, end, text, lp = seg[:4]
        toks = tokens(text)
        probs = word_probs(text, seg[4] if len(seg) > 4 else None)
        after_question, asked = asked, asks_for_call(text)
        if STRICT and lp < MIN_LOGPROB or after_question:
            continue
        window = [(t, w) for t, w in window if start - t <= 30] + [(start, toks)]
        recent_cq = any(w in CQ_WORDS or w == 'qrz' for _t, ws in window for w in ws)
        prev = None                                   # (call, last token) of the previous runner copy
        for call, a, b in calls_in(toks):
            if prev and prev[0] == call and prev[1] + 1 == a:
                if STRICT and probs is not None and unsure(toks, probs, a, b):
                    continue
                hits.append((start, call, 'doubled'))   # "AA3B AA3B": the repeat counts, as in CW
                prev = (call, b)
                continue
            before = toks[max(0, a - 3):a]
            after = toks[b + 1:b + 4]
            given_report = bool(set(after[:2]) & CALLER_AFTER)       # "<call>, you're 59": being worked
            caller = bool(set(before) & CALLER_BEFORE) or given_report
            if a >= 1 and toks[a - 1] in CQ_WORDS | {'contest', 'qrz', 'de'} and not given_report:
                caller = False                  # "..., thanks. Contest, WF8Z": the runner signing
            if b + 1 < len(toks) and toks[b + 1] in ('contest', 'qrz') and not given_report:
                caller = False                  # "Thanks, WF8Z, contest": signing, the thanks is to the caller
            this_is = self_id(toks, a)
            pairs = {' '.join(after[k:k + 2]) for k in range(len(after) - 1)}
            contest_next = (a >= 1 and toks[a - 1] == 'contest') or (b + 1 < len(toks) and toks[b + 1] == 'contest')
            runner = (this_is and recent_cq) or bool(set(after) & RUNNER_AFTER) or contest_next or after_cq(toks, a) or \
                bool(pairs & {'calling cq', 'standing by'}) or \
                (a >= 1 and toks[a - 1] in CQ_WORDS)
            if STRICT:     # a CQ in the last 30 s, or "this is <call> ... QRZ" (the runner between QSOs)
                qrz_after = 'qrz' in toks[b + 1:]
                runner = runner and (any(w in CQ_WORDS or w == 'qrz' for _t, ws in window for w in ws)
                                     or contest_next or after_cq(toks, a))   # CQ or QRZ in the last 30 s, or "contest WF8Z contest"
            if STRICT and probs is not None and unsure(toks, probs, a, b):
                continue                                # not a clean copy: wait for the next one
            if given_report:
                continue
            if runner and not (caller and not this_is):
                hits.append((start, call, ' '.join(toks[max(0, a - 4):b + 4])))
                prev = (call, b)
    return hits


US_CALL = re.compile(r'^(?:[KNW][A-Z]?|A[A-L])[0-9]')     # a US prefix: must then be an issued call
ONE_BY_ONE = re.compile(r'^[KNW][0-9][A-Z]$')          # special-event calls, not in the FCC database
LONG_CALL = re.compile(r'[0-9][A-Z]{4,}$')              # longer than a 2x3 (Fred 10-21: rare, special events)


def copies_needed(base, fcc):
    """Clean runner copies a call needs: 3 for a 1x1 (can't be checked against the FCC list) and for a
    call longer than a 2x3 (rare; a stutter like KC5SCKK makes them); 2 otherwise."""
    if LONG_CALL.search(base) or (ONE_BY_ONE.match(base) and fcc is not None):
        return 3
    return 2


def load_fcc():
    """--fcc FILE: active US calls; a US-format call not in it is not spotted (1x1s excepted)."""
    if '--fcc' not in sys.argv:
        return None
    return set(open(sys.argv[sys.argv.index('--fcc') + 1]).read().split())


def us_call_ok(base, fcc):
    return fcc is None or not US_CALL.match(base) or ONE_BY_ONE.match(base) or base in fcc


VARIANTS = _os.environ.get('VS_VARIANTS', '1') != '0'   # outvoted() in place of cut_off() (10-24)
# dominant call on a carrier (10-24, Fred: a contest runner mostly names the CALLER, "K3NM 59 08,
# thanks, QRZ", and signs "thanks, P40W" or "take care, P40W" now and then): within DOM_S on one
# 0.5 kHz carrier, the call named in the most transmissions (not counting a name followed by a
# report, which is the runner talking TO a caller) owns the frequency when it is named DOM_MIN times,
# DOM_RATIO times as often as the next call, and the carrier has contest words there
DOMINANT = _os.environ.get('VS_DOMINANT', '1') != '0'
DOM_S, DOM_MIN, DOM_RATIO = 600.0, 4, 3.0
CONTEST_WORDS = {'contest', 'qrz', 'cq', 'cqcq'}
VARIANT_S = float(_os.environ.get('VS_VARIANT_S', 1800))  # a variant on the same carrier within 30 min is the same station
POOL_KHZ, POOL_S = 0.5, float(_os.environ.get('VS_POOL_S', 600))   # copies within 500 Hz and 10 min count together


def contained(short, long_):
    """short is long_ with one or two characters left out (KC5CK in KC5SCK, KF4I in KF4IZE)."""
    if not 1 <= len(long_) - len(short) <= 2:
        return False
    it = iter(long_)
    return all(ch in it for ch in short)


def cut_off(g, groups, fcc=None):
    """A longer call that contains g's is heard twice or more on its carrier around the same time:
    g's is probably that call with a letter lost, so g is not spotted (precision, not a repair).
    One copy of the longer call is not enough ("...Charlie Kilo, Kilo, CQ" once made KC5SCKK), and the
    longer call must itself be spottable: a never-issued US call (KC5SCKK twice) cuts nothing off."""
    return any(h is not g and h['n'] >= 2 and abs(h['khz'] - g['khz']) <= POOL_KHZ and contained(g['base'], h['base'])
               and us_call_ok(h['base'], fcc)
               and h['first'] - POOL_S <= g['last'] and g['first'] - POOL_S <= h['last'] for h in groups)


# "Again?", "say again", "I need your call again", "what's your call?" (Fred 10-24: a question for a call;
# in contest traffic mostly the runner asking a caller, 586 "again"s and 115 "your call"s in two hours):
# the next transmission is a caller saying his own call, so it is neither a runner copy nor an owner mention
ASKS = _os.environ.get('VS_ASKS', '1') != '0'
_ASK_RE = re.compile(r"\b(your call|what'?s your call|call ?sign\b.*\?|say again|call again)\b|\bagain\W*$", re.I)


def asks_for_call(text):
    return bool(ASKS and _ASK_RE.search(text.strip()))


def seg_info(text):
    """One transcribed segment, read once: (every call heard [base], calls that count as a mention for the
    dominant rule [base, once each, not followed by a report, none if Whisper looped], contest words there).
    Shared by the offline scorer (main) and the live runner."""
    tk = tokens(text)
    looping = len(tk) >= 8 and len(set(tk)) < 0.4 * len(tk)      # "K 2 K 2 K 2 ..."
    heard, named = [], []
    for c, _a, _b in calls_in(tk):
        b = c.split('/')[0]
        heard.append(b)
        after = tk[_b + 1:_b + 3]
        report = any(w in ('59', '599', '5', 'five', 'fifty') or re.fullmatch(r'59\d{1,2}', w) for w in after)
        if not report and not looping and b not in named:
            named.append(b)
    return heard, named, bool(CONTEST_WORDS & set(tk))


def dominant(ms, contest_ts, t, fcc=None):
    """The call that owns a carrier in [t, t + DOM_S): ms = [(time, call)] mentions there, contest_ts the
    times with contest words. (call, n, runner_up) or None; see DOMINANT."""
    cnt = {}
    for tm, c in ms:
        if t <= tm < t + DOM_S:
            cnt[c] = cnt.get(c, 0) + 1
    if not cnt or not any(t <= x < t + DOM_S for x in contest_ts):
        return None
    top = sorted(cnt.items(), key=lambda kv: -kv[1])
    c, n = top[0]
    runner_up = top[1][1] if len(top) > 1 else 0
    if n < DOM_MIN or n < DOM_RATIO * runner_up or not known(c, fcc) or n < copies_needed(c, fcc):
        return None
    return c, n, runner_up


def variant(a, b):
    """a and b are one call heard two ways: one has a character or two more (D5D/ED5D, VE9CF/VE9CFF,
    PY2NY/PY2NYPY) or one character differs (CB1B/1B1B, NL7WA/3L7WA, VE7ZO/VE70O)."""
    if a == b:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = sorted((a, b), key=len)
    return contained(short, long_) or long_.startswith(short) and len(long_) - len(short) <= 2


_SCP = None


def known(call, fcc):
    """In MASTER.SCP (VS_SCP, default the repository's MASTER.SCP), or for a US call the FCC list."""
    global _SCP
    if _SCP is None:
        path = _os.environ.get('VS_SCP', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'MASTER.SCP'))
        try:
            _SCP = {l.strip() for l in open(path) if l.strip() and not l.startswith(('#', '!'))}
        except OSError:
            _SCP = set()
    return call in _SCP or bool(fcc) and call in fcc


def outvoted(g, groups, fcc=None, heard_n=None):
    """A variant of g's call heard on g's carrier around the same time is the better reading, so g is
    not spotted (10-24, CQ WW hour: 6 of 8 busts sat beside the real call). Better = a known call
    (SCP, or FCC for a US call) over an unknown one (HI8R over I8R, S53M over MS53M); else a call that
    only adds letters in front (TO1O over O1O); else more runner copies (CB1B 25 over 1B1B 2); a tie keeps
    the old rule, the longer call wins. Chooses between two
    calls actually heard; never makes one up."""
    kg = known(g['base'], fcc)
    if not kg and heard_n:
        # a known variant heard on this carrier twice or more, in any position (TF3SG beside F3SG,
        # PY2NY beside PY2NYPY): the unknown one is the misheard copy. Only suppresses g.
        for c, n in heard_n.get(round(g['khz'] * 2) / 2, {}).items():
            if n >= 2 and variant(g['base'], c) and known(c, fcc):
                return True
    for h in groups:
        if h is g or abs(h['khz'] - g['khz']) > POOL_KHZ or not variant(g['base'], h['base']):
            continue
        if not (h['first'] - VARIANT_S <= g['last'] and g['first'] - VARIANT_S <= h['last']) or h['n'] < 2:
            continue
        kh = known(h['base'], fcc)
        if kh != kg:
            if kh:
                return True
            continue
        # Whisper often loses a runner's first letter (I8R for HI8R, O1O for TO1O), so the clipped form
        # can have more copies: a longer call that only adds letters in front wins whatever the counts
        if len(h['base']) > len(g['base']) and h['base'].endswith(g['base']):
            return True
        if len(g['base']) > len(h['base']) and g['base'].endswith(h['base']):
            continue                      # and the clipped form never outvotes the full call
        if h['n'] > g['n'] or h['n'] == g['n'] and len(h['base']) > len(g['base']):
            return True
    return False



SAME_KHZ = float(_os.environ.get('VS_SAME_KHZ', 3.0))   # one spot per call within this (10-26)
LOCKON = _os.environ.get('VS_LOCKON', '1') != '0'      # caller_on_owned() (10-26)
OWN_RATIO, OWN_S = 2.0, 330.0     # an owner: twice the copies, heard within 330 s (a lock period + its check)


def same_spot(sent, base, khz, t, respot_s=None):
    """base was already spotted within SAME_KHZ of khz (and, with respot_s, less than respot_s ago).
    sent: [(base, khz, t)]. One SSB voice can make two or three carriers a kHz or two apart (K9CT at
    3736.9, 3737.9 and 3739.8; ED1R at 7117.3 and 7117.9 in the CQ WW hours), one spot each."""
    return any(b == base and abs(k - khz) <= SAME_KHZ and (respot_s is None or t - ts < respot_s)
               for b, k, ts in sent)


def caller_on_owned(g, groups):
    """Lock-on for voice: g is a station answering the runner who owns g's carrier, not a runner
    (KF0WYR: 2 copies on W1RCR's 14228.2 while W1RCR ran there, 136 copies; KF0WYR was searching and
    pouncing on 14233, 14305 and 14284 too). The owner is another call on the carrier with a copy in the
    OWN_S before g's first copy and OWN_RATIO times g's copies within OWN_S of g's. A runner who takes a
    frequency its owner has left gets no owner copies before it and is spotted as usual."""
    for h in groups:
        if h is g or h['base'] == g['base'] or abs(h['khz'] - g['khz']) > POOL_KHZ:
            continue
        if variant(g['base'], h['base']) or contained(g['base'], h['base']) or contained(h['base'], g['base']):
            continue                      # another reading of the same station (I1X of II1X, J62KJ of J62K)
        ts = h.get('ts') or [h['first'], h['last']]
        before = any(g['first'] - OWN_S <= x <= g['first'] for x in ts)
        near = sum(1 for x in ts if g['first'] - OWN_S <= x <= g['last'] + OWN_S)
        if before and near >= OWN_RATIO * g['n']:
            return h['base']              # truthy: the owner, for --debug
    return None


def main():
    dumpdir, model = sys.argv[1], sys.argv[2]
    debug = '--debug' in sys.argv
    hits, heard, worked, heard_n = [], {}, {}, {}   # heard_n: 0.5 kHz carrier -> call -> segments naming it
    mentions, contest_at = {}, {}                     # cell -> [(t, call, khz, text)] not followed by a report; cell -> [t]
    for path in sorted(glob.glob(os.path.join(dumpdir, 'ssb_*.%s.json' % model))):
        name = os.path.basename(path)[len('ssb_'):-len('.%s.json' % model)]
        khz, _, off = name.partition('@')            # track clips: ssb_<kHz>@<start s>
        khz, off = float(khz), float(off or 0)
        segs = json.load(open(path))
        hits += [(khz, t + off, c, x) for t, c, x in runner_hits(segs)]
        worked.setdefault(round(khz * 2) / 2, set()).update(worked_calls(segs))
        asked = False
        for sg in segs:
            cell = round(khz * 2) / 2
            all_calls, named, contest = seg_info(sg[2])
            if asked:
                named = []                  # the answer to "again?" / "your call?" is a caller
            asked = asks_for_call(sg[2])
            if contest:
                contest_at.setdefault(cell, []).append(sg[0] + off)
            hn = heard_n.setdefault(cell, {})
            for b in all_calls:
                hn[b] = hn.get(b, 0) + 1
                if debug:
                    heard.setdefault(khz, set()).add(b)
            for b in named:
                mentions.setdefault(cell, []).append((sg[0] + off, b, khz, sg[2]))
    # pool copies across clips: same base call, carriers within POOL_KHZ, times within POOL_S
    groups = []
    for khz, t, call, ctx in sorted(hits, key=lambda h: h[1]):
        b = call.split('/')[0]
        for g in groups:
            if g['base'] == b and abs(g['khz'] - khz) <= POOL_KHZ and t - g['last'] <= POOL_S:
                g['n'] += 1
                g['last'] = t
                g['ts'].append(t)
                break
        else:
            groups.append({'base': b, 'call': call, 'khz': khz, 'first': t, 'last': t, 'n': 1, 'ctx': ctx, 'ts': [t]})
    spotted = set()
    sent = []                              # (base, kHz, t) spotted: same_spot()
    fcc = load_fcc()
    for g in sorted(groups, key=lambda g: -g['n']):    # the best-supported carrier of a call is the one spotted
        if g['n'] < copies_needed(g['base'], fcc) and g['n'] >= 2:
            if debug:
                print('     %9.1f  %-10s %d copies, needs %d' % (g['khz'], g['call'], g['n'], copies_needed(g['base'], fcc)))
            continue                       # 1x1s and calls longer than a 2x3: three clean copies
        if g['n'] >= 2 and (outvoted(g, groups, fcc, heard_n) if VARIANTS else cut_off(g, groups, fcc)):
            if debug:
                print('     %9.1f  %-10s a variant of it has more copies here: not spotted' % (g['khz'], g['call']))
            continue
        owner = g['n'] >= 2 and LOCKON and caller_on_owned(g, groups)
        if owner:
            if debug:
                print('     %9.1f  %-10s heard on a carrier %s runs: a caller, not spotted' % (g['khz'], g['call'], owner))
            continue
        if g['n'] >= 2 and not us_call_ok(g['base'], fcc):
            if debug:
                print('     %9.1f  %-10s not an issued US call: not spotted' % (g['khz'], g['call']))
            continue
        runfreq = '--runfreq' in sys.argv and len(worked.get(round(g['khz'] * 2) / 2, ())) >= RUN_FREQ_MIN \
            and g['base'] not in worked.get(round(g['khz'] * 2) / 2, ())
        if (g['n'] >= 2 or runfreq) and (g['base'], round(g['khz'])) not in spotted \
                and not same_spot(sent, g['base'], g['khz'], g['first']):
            if g['n'] < 2:
                g['ctx'] += '  [run frequency: reports to %d calls]' % len(worked[round(g['khz'] * 2) / 2])
            spotted.add((g['base'], round(g['khz'])))
            sent.append((g['base'], g['khz'], g['first']))
            print('SPOT %9.1f  %-10s heard as runner %d times, first at %.0f s: "%s"'
                  % (g['khz'], g['call'], g['n'], g['first'], g['ctx']))
        elif debug and g['n'] < 2:
            print('     %9.1f  %-10s once: "%s"' % (g['khz'], g['call'], g['ctx']))
    if DOMINANT:
        for cell, ms in sorted(mentions.items()):
            ms.sort()
            for i, (t, b, khz, txt) in enumerate(ms):
                d = dominant([(m[0], m[1]) for m in ms], contest_at.get(cell, ()), t, fcc)
                if d is None or d[0] != b:
                    continue
                c, n, runner_up = d
                if (c, round(khz)) in spotted or same_spot(sent, c, khz, t):
                    continue
                pseudo = {'base': c, 'khz': khz, 'first': t, 'last': t + DOM_S, 'n': n}
                if outvoted(pseudo, groups, fcc, heard_n):
                    continue
                spotted.add((c, round(khz)))
                sent.append((c, khz, t))
                print('SPOT %9.1f  %-10s heard as runner %d times, first at %.0f s: "%s  [dominant: named %d times, next call %d]"'
                      % (khz, c, n, t, ' '.join(tokens(txt))[:80], n, runner_up))
    if debug:
        for khz in sorted(heard):
            print('     %9.1f  calls heard: %s' % (khz, ' '.join(sorted(heard[khz]))))


if __name__ == '__main__':
    main()
