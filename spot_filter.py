#!/usr/bin/env python3
"""
spot_filter.py — Filter csdr-cwskimmer output through master.scp validation.

Reads csdr-cwskimmer output (freq:wpm:text), extracts callsigns found near
CQ/TEST patterns, validates against master.scp, and outputs confirmed spots.

Usage:
    cat audio.wav | csdr-cwskimmer -r 48000 -i -n 16 | python3 spot_filter.py
"""

import re
import sys
from collections import defaultdict

# Callsign pattern: 1-2 letters/digits, 1-2 digits, 1-3 letters
CALL_RE = re.compile(
    r'('
    r'[A-Z0-9]{1,2}\d{1,2}[A-Z]{1,3}'
    r'|\d[A-Z]\d[A-Z]{1,3}'
    r'|\d[A-Z]{2}\d[A-Z]{1,3}'
    r')'
)

# CQ/TEST trigger patterns — station must be calling CQ or TEST
CQ_PATTERNS = re.compile(
    r'\b(CQ|TEST|QRZ|FD|SS|NA)\b', re.IGNORECASE
)


def load_master_scp(filename='MASTER.SCP'):
    """Load master.scp into a set for O(1) lookup."""
    calls = set()
    with open(filename) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                calls.add(line.upper())
    return calls


def extract_callsigns(text):
    """Extract all callsign-shaped strings from decoded text."""
    found = set()
    for match in CALL_RE.finditer(text.upper()):
        found.add(match.group(1))
    return found


def main():
    master = load_master_scp()
    print(f"Loaded {len(master)} callsigns from MASTER.SCP", file=sys.stderr)

    spot_count = 0
    raw_count = 0
    no_cq_count = 0
    no_match_count = 0
    seen = defaultdict(int)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        raw_count += 1

        # Parse csdr-cwskimmer output: freq:wpm:text
        parts = line.split(':', 2)
        if len(parts) < 3:
            continue

        try:
            freq_offset = int(parts[0])
            wpm = int(parts[1])
        except ValueError:
            continue

        text = parts[2].upper()

        # Must contain CQ, TEST, or similar trigger
        if not CQ_PATTERNS.search(text):
            no_cq_count += 1
            continue

        # Extract callsign candidates
        candidates = extract_callsigns(text)

        # Remove trigger words that look like callsigns (if any)
        candidates.discard('CQ')
        candidates.discard('TEST')
        candidates.discard('QRZ')

        # Validate against master.scp
        matched = False
        for call in candidates:
            if call in master:
                key = (call, freq_offset)
                seen[key] += 1
                if seen[key] == 1:
                    spot_count += 1
                    print(f"SPOT: {freq_offset:6d} Hz  {wpm:2d} WPM  {call:<10s}  [{text.strip()}]")
                    sys.stdout.flush()
                matched = True

        if not matched and candidates:
            no_match_count += 1

    unique_calls = len(set(c for c, f in seen))
    print(f"\nSummary:", file=sys.stderr)
    print(f"  Raw decodes:      {raw_count}", file=sys.stderr)
    print(f"  No CQ/TEST:       {no_cq_count} (filtered)", file=sys.stderr)
    print(f"  No SCP match:     {no_match_count} (had calls, none in master.scp)", file=sys.stderr)
    print(f"  Valid spots:      {spot_count}", file=sys.stderr)
    print(f"  Unique callsigns: {unique_calls}", file=sys.stderr)


if __name__ == '__main__':
    main()
