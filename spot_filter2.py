#!/usr/bin/env python3
"""
spot_filter2.py — Advanced master.scp filter for CW skimmer output.

Two modes:
  --strict: Require CQ/TEST pattern (original behavior)
  --relaxed: Accept any callsign found 2+ times across decode lines (default)

Both modes validate against master.scp.
"""

import re
import sys
from collections import defaultdict

_CALL_BASE = (
    r'[A-Z0-9]{1,2}\d{1,2}[A-Z]{1,3}'
    r'|\d[A-Z]\d[A-Z]{1,3}'
    r'|\d[A-Z]{2}\d[A-Z]{1,3}'
)

# Standard callsigns (no slash)
CALL_RE = re.compile(
    r'(?<![A-Z0-9/])'
    r'(' + _CALL_BASE + r')'
    r'(?![A-Z0-9/])'
)

# Slash calls: PREFIX/CALL or CALL/SUFFIX
# e.g., PJ2/AG3I, F/DL3HAH, W1AW/4, JA0XQO/1, IT9/DK6XZ
# PREFIX can be 1-4 chars (F, DL, PJ2, IT9), SUFFIX can be call or 1-4 chars
CALL_SLASH_RE = re.compile(
    r'(?<![A-Z0-9])'
    r'([A-Z0-9]{1,4}/(?:' + _CALL_BASE + r')'   # PREFIX/CALL
    r'|(?:' + _CALL_BASE + r')/[A-Z0-9]{1,4})'   # CALL/SUFFIX
    r'(?![A-Z0-9])'
)

# 1x1 special event calls: W1A, K3I, N4B etc. (letter + digit + letter)
CALL_1X1_RE = re.compile(
    r'(?<![A-Z0-9])'
    r'([AKNW]\d[A-Z])'
    r'(?![A-Z0-9])'
)

MIN_CALL_LEN = 4        # Minimum callsign length for standard calls
SNR_STRONG = 15          # dB threshold for strong signal (1 decode enough)

CQ_PATTERNS = re.compile(r'(CQ|TEST|QRZ|CWT|SST|FD|SS|CQCQ|CQTEST|CQCWT)', re.IGNORECASE)
# DXpedition/contest patterns
DX_PATTERNS = re.compile(r'\b(TU|UP|DE|K|BK|GE|GM|GA|UR|FB|NR|AGN)\b', re.IGNORECASE)

# Common false positives that match callsign regex but aren't real calls
FALSE_POSITIVES = {
    'CQ', 'TEST', 'QRZ', 'DE', 'TU', '5NN', '599', 'RST',
    'QSL', 'QTH', 'QRL', 'CFM', 'PSE', 'TNX', 'TKS', 'HW',
    'BT', 'AR', 'SK', 'KN', 'AS',
    # Common garbled patterns that look like calls
    'EE5E', 'TT5T', 'NN5N', 'SS5S', 'AA5A',
}


def remove_noise_letters(text):
    """Remove isolated noise characters from decoded text.
    E (dit) and I (di-dit) are the most common false decodes from noise.
    Also removes T (dah), M (dah-dah), and A (di-dah) when isolated."""
    # Replace isolated single noise chars (E, I, T, M, A)
    text = re.sub(r'(?<![A-Z0-9])([EITMA])(?![A-Z0-9])', ' ', text)
    # Remove click artifacts: isolated punctuation and special chars
    text = re.sub(r'[_?<>()\[\]{}|&]', ' ', text)
    # Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def load_blacklist(filename='blacklist.txt'):
    """Load blacklisted callsigns to never spot."""
    calls = set()
    try:
        with open(filename) as f:
            for line in f:
                line = line.strip().upper()
                if line and not line.startswith('#'):
                    calls.add(line)
    except FileNotFoundError:
        pass
    return calls

def load_master_scp(filename='MASTER.SCP', supplement='add_calls.txt'):
    """Load master.scp + optional supplementary callsign file."""
    calls = set()
    with open(filename) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                calls.add(line.upper())
    # Load supplementary calls (new DXpeditions, etc.)
    try:
        with open(supplement) as f:
            added = 0
            for line in f:
                line = line.strip().upper()
                if line and not line.startswith('#') and line not in calls:
                    calls.add(line)
                    added += 1
            if added:
                print(f"  + {added} supplementary calls from {supplement}", file=sys.stderr)
    except FileNotFoundError:
        pass
    return calls

def extract_callsigns(text):
    found = set()
    text = text.upper()
    # Slash calls first (PREFIX/CALL or CALL/SUFFIX)
    for match in CALL_SLASH_RE.finditer(text):
        call = match.group(1)
        if len(call) >= 5:  # minimum: X1X/X = 5 chars
            found.add(call)
    # Standard callsigns (4+ chars)
    for match in CALL_RE.finditer(text):
        call = match.group(1)
        if call in FALSE_POSITIVES:
            continue
        if len(call) < MIN_CALL_LEN:
            continue
        found.add(call)
    # 1x1 special event calls (3 chars: letter + digit + letter)
    for match in CALL_1X1_RE.finditer(text):
        call = match.group(1)
        if call not in FALSE_POSITIVES:
            found.add(call)
    return found

def main():
    strict = '--strict' in sys.argv
    master = load_master_scp()
    blacklist = load_blacklist()
    print(f"Loaded {len(master)} callsigns from MASTER.SCP", file=sys.stderr)
    if blacklist:
        print(f"Loaded {len(blacklist)} blacklisted callsigns", file=sys.stderr)
    print(f"Mode: {'strict (CQ/TEST required)' if strict else 'relaxed (2+ occurrences)'}", file=sys.stderr)

    # Collect all decode lines with noise letter removal
    all_lines = []
    for line in sys.stdin:
        line = line.strip()
        if not line or ':' not in line:
            continue
        parts = line.split(':', 2)
        if len(parts) < 3:
            continue
        try:
            freq = int(parts[0])
            wpm = int(parts[1])
        except ValueError:
            continue
        text = remove_noise_letters(parts[2].upper())
        all_lines.append((freq, wpm, text, line))

    # Extract all callsign sightings — track both SCP and non-SCP calls
    call_sightings = defaultdict(list)      # SCP-validated calls
    noscp_sightings = defaultdict(list)     # calls NOT in SCP (for tier 3)
    FREQ_TOLERANCE = 1000                   # Hz tolerance for frequency clustering (wide for multi-BW)
    SCP_CONSISTENT_FREQ = 2                 # sightings for SCP call with no context
    NOSCP_CONSISTENT_FREQ = 10             # sightings at same freq for non-SCP call

    for freq, wpm, text, line in all_lines:
        candidates = extract_callsigns(text)
        for call in candidates:
            if call in blacklist:
                continue
            if call in master:
                call_sightings[call].append((freq, wpm, text, line))
            else:
                noscp_sightings[call].append((freq, wpm, text, line))

    def max_freq_cluster(sightings, tolerance=FREQ_TOLERANCE):
        """Find the largest cluster of sightings at a consistent frequency."""
        if not sightings:
            return 0, 0
        freqs = sorted(s[0] for s in sightings)
        best_count = 0
        best_freq = freqs[0]
        for f in freqs:
            count = sum(1 for f2 in freqs if abs(f2 - f) <= tolerance)
            if count > best_count:
                best_count = count
                best_freq = f
        return best_count, best_freq

    # Contest exchange patterns that suggest a real station
    EXCHANGE_RE = re.compile(r'\b(5NN|599|5N|RST|HQ|NR|NE)\b', re.IGNORECASE)

    # Apply filtering
    valid_spots = []
    tier_counts = defaultdict(int)

    if strict:
        # Strict: need CQ/TEST in the line
        for call, sightings in call_sightings.items():
            for freq, wpm, text, line in sightings:
                if CQ_PATTERNS.search(text):
                    valid_spots.append((call, freq, wpm, text))
                    break
    else:
        for call, sightings in call_sightings.items():
            has_cq = any(CQ_PATTERNS.search(text) for _, _, text, _ in sightings)
            has_dx = any(DX_PATTERNS.search(text) for _, _, text, _ in sightings)
            has_exchange = any(EXCHANGE_RE.search(text) for _, _, text, _ in sightings)
            n_sightings = len(sightings)
            freq_count, best_freq = max_freq_cluster(sightings)

            # Tier 1: CQ/TEST/CWT context — high confidence
            if has_cq:
                best = max(sightings, key=lambda s: len(s[2]))
                valid_spots.append((call, best[0], best[1], best[2]))
                tier_counts['T1_context'] += 1
            # Tier 1b: DXpedition pattern + 2+ sightings
            elif has_dx and n_sightings >= 2:
                best = max(sightings, key=lambda s: len(s[2]))
                valid_spots.append((call, best[0], best[1], best[2]))
                tier_counts['T1b_dxped'] += 1
            # Tier 1c: Contest exchange + 5+ char call
            elif has_exchange and len(call) >= 5:
                best = max(sightings, key=lambda s: len(s[2]))
                valid_spots.append((call, best[0], best[1], best[2]))
                tier_counts['T1c_exchange'] += 1
            # Tier 2: In SCP, no context, 3+ sightings (trust database)
            elif n_sightings >= SCP_CONSISTENT_FREQ:
                best = max(sightings, key=lambda s: len(s[2]))
                valid_spots.append((call, best[0], best[1], best[2]))
                tier_counts['T2_scp_multi'] += 1
            # Tier 2b: In SCP, 2 sightings + 5+ char call
            elif n_sightings >= 2 and len(call) >= 5:
                best = max(sightings, key=lambda s: len(s[2]))
                valid_spots.append((call, best[0], best[1], best[2]))
                tier_counts['T2b_scp_long'] += 1

        # Tier 3: NOT in SCP, consistent frequency with N+ sightings
        for call, sightings in noscp_sightings.items():
            if len(call) < 4:
                continue
            freq_count, best_freq = max_freq_cluster(sightings)
            if freq_count >= NOSCP_CONSISTENT_FREQ:
                near = [s for s in sightings if abs(s[0] - best_freq) <= FREQ_TOLERANCE]
                best = max(near, key=lambda s: len(s[2]))
                valid_spots.append((call, best[0], best[1], best[2]))
                tier_counts['T3_noscp_freq'] += 1

    if tier_counts:
        for tier, count in sorted(tier_counts.items()):
            print(f"  {tier}: {count} calls", file=sys.stderr)

    # Sort by frequency
    valid_spots.sort(key=lambda x: x[1])

    # Print
    for call, freq, wpm, text in valid_spots:
        print(f"SPOT: {freq:6d} Hz  {wpm:2d} WPM  {call:<10s}  [{text.strip()}]")

    unique_calls = set(s[0] for s in valid_spots)
    print(f"\nSummary:", file=sys.stderr)
    print(f"  Raw lines:        {len(all_lines)}", file=sys.stderr)
    print(f"  Calls in master:  {len(call_sightings)}", file=sys.stderr)
    print(f"  Valid spots:      {len(valid_spots)}", file=sys.stderr)
    print(f"  Unique callsigns: {len(unique_calls)}", file=sys.stderr)

    # Print calls NOT found for comparison
    if '--diff' in sys.argv:
        print(f"\n  Validated calls: {' '.join(sorted(unique_calls))}", file=sys.stderr)

if __name__ == '__main__':
    main()
