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

CALL_RE = re.compile(
    r'(?<![A-Z0-9])'   # negative lookbehind — don't match inside longer strings
    r'('
    r'[A-Z0-9]{1,2}\d{1,2}[A-Z]{1,3}'
    r'|\d[A-Z]\d[A-Z]{1,3}'
    r'|\d[A-Z]{2}\d[A-Z]{1,3}'
    r')'
    r'(?![A-Z0-9])'    # negative lookahead
)

MIN_CALL_LEN = 4        # Minimum callsign length to reduce false positives

CQ_PATTERNS = re.compile(r'\b(CQ|TEST|QRZ)\b', re.IGNORECASE)

def load_master_scp(filename='MASTER.SCP'):
    calls = set()
    with open(filename) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                calls.add(line.upper())
    return calls

def extract_callsigns(text):
    found = set()
    for match in CALL_RE.finditer(text.upper()):
        call = match.group(1)
        # Skip common false positives
        if call in ('CQ', 'TEST', 'QRZ', 'DE', 'TU', '5NN', '599', 'RST'):
            continue
        # Minimum length filter
        if len(call) < MIN_CALL_LEN:
            continue
        found.add(call)
    return found

def main():
    strict = '--strict' in sys.argv
    master = load_master_scp()
    print(f"Loaded {len(master)} callsigns from MASTER.SCP", file=sys.stderr)
    print(f"Mode: {'strict (CQ/TEST required)' if strict else 'relaxed (2+ occurrences)'}", file=sys.stderr)

    # Collect all decode lines
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
        text = parts[2].upper()
        all_lines.append((freq, wpm, text, line))

    # Extract all callsign sightings
    call_sightings = defaultdict(list)  # call -> [(freq, wpm, text, line), ...]

    for freq, wpm, text, line in all_lines:
        candidates = extract_callsigns(text)
        for call in candidates:
            if call in master:
                call_sightings[call].append((freq, wpm, text, line))

    # Contest exchange patterns that suggest a real station
    EXCHANGE_RE = re.compile(r'\b(5NN|599|5N|RST|HQ|NR|NE)\b', re.IGNORECASE)

    # Apply filtering
    valid_spots = []

    if strict:
        # Strict: need CQ/TEST in the line
        for call, sightings in call_sightings.items():
            for freq, wpm, text, line in sightings:
                if CQ_PATTERNS.search(text):
                    valid_spots.append((call, freq, wpm, text))
                    break  # one per call
    else:
        # Relaxed: need 2+ sightings OR CQ/TEST context OR contest exchange
        for call, sightings in call_sightings.items():
            has_cq = any(CQ_PATTERNS.search(text) for _, _, text, _ in sightings)
            has_exchange = any(EXCHANGE_RE.search(text) for _, _, text, _ in sightings)
            # Confidence tiers:
            # Tier 1: CQ/TEST in context — high confidence, accept any length
            # Tier 2: 3+ sightings across different lines — medium confidence
            # Tier 3: Contest exchange + 5+ char call — medium confidence
            # Tier 4: 2 sightings + 5+ char call — lower but acceptable
            tier1 = has_cq
            tier2 = len(sightings) >= 3
            tier3 = has_exchange and len(call) >= 5
            tier4 = len(sightings) >= 2 and len(call) >= 5
            if tier1 or tier2 or tier3 or tier4:
                # Pick best sighting (longest text with the call)
                best = max(sightings, key=lambda s: len(s[2]))
                freq, wpm, text, line = best
                valid_spots.append((call, freq, wpm, text))

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
