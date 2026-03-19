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
SNR_STRONG = 15          # dB threshold for strong signal (1 decode enough)

CQ_PATTERNS = re.compile(r'\b(CQ|TEST|QRZ)\b', re.IGNORECASE)
# DXpedition patterns: "TU [CALL]", "[CALL] UP", "DE [CALL]"
DX_PATTERNS = re.compile(r'\b(TU|UP|DE|K|BK)\b', re.IGNORECASE)


def remove_noise_letters(text):
    """Remove isolated E and I characters (SDC 'Remove Noise Letters' feature).
    E (dit) and I (di-dit) are the most common false decodes from noise,
    birdies, and digital mode signals."""
    # Replace isolated E and I (surrounded by spaces or at start/end)
    text = re.sub(r'(?<![A-Z0-9])([EI])(?![A-Z0-9])', ' ', text)
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

    # Extract all callsign sightings
    call_sightings = defaultdict(list)  # call -> [(freq, wpm, snr_db, text, line), ...]

    for freq, wpm, text, line in all_lines:
        candidates = extract_callsigns(text)
        # Parse SNR from WPM field (our decoder outputs log10(snr)*20)
        snr_db = wpm  # wpm field actually contains SNR in our format
        for call in candidates:
            if call in master and call not in blacklist:
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
        # Relaxed with tiered verification (SDC-inspired)
        for call, sightings in call_sightings.items():
            has_cq = any(CQ_PATTERNS.search(text) for _, _, text, _ in sightings)
            has_dx = any(DX_PATTERNS.search(text) for _, _, text, _ in sightings)
            has_exchange = any(EXCHANGE_RE.search(text) for _, _, text, _ in sightings)
            n_sightings = len(sightings)

            # Confidence tiers (SDC-inspired tiered verification):
            # Tier 1: CQ/TEST in context — high confidence, accept any length
            # Tier 2: DXpedition pattern (TU/UP/DE) + 2+ sightings — DXpedition mode
            # Tier 3: 3+ sightings across different lines — medium confidence
            # Tier 4: Contest exchange + 5+ char call — medium confidence
            # Tier 5: 2 sightings + 5+ char call — lower but acceptable
            tier1 = has_cq
            tier2 = has_dx and n_sightings >= 2
            tier3 = n_sightings >= 3
            tier4 = has_exchange and len(call) >= 5
            tier5 = n_sightings >= 2 and len(call) >= 5

            if tier1 or tier2 or tier3 or tier4 or tier5:
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
