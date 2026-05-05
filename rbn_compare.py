#!/usr/bin/env python3
"""Compare SparkGap spots against RBN data for a given time window.

Usage:
    python3 rbn_compare.py <our_spots.log> <rbn.csv> [--start HH:MM] [--end HH:MM] [--band 20m]

Examples:
    python3 rbn_compare.py contest_logs/cwt_20260422_1900z_20m.log rbn_data/20260422.csv
    python3 rbn_compare.py contest_logs/cwt_20260422_1900z_20m.log rbn_data/20260422.csv --start 19:00 --end 20:00 --band 20m
"""
import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path


def parse_our_spots(path):
    """Parse SparkGap spot log lines."""
    spots = []
    pat = re.compile(r'(\d{2}:\d{2}:\d{2}).*SPOT:\s+([\d.]+)\s+(\S+)\s+(\d+)\s+dB\s+(\d+)\s+WPM\s+\[(\w+)\]')
    with open(path) as f:
        for line in f:
            m = pat.search(line)
            if m:
                spots.append({
                    'time': m.group(1),
                    'freq': float(m.group(2)),
                    'call': m.group(3),
                    'db': int(m.group(4)),
                    'wpm': int(m.group(5)),
                    'method': m.group(6),
                })
    return spots


def strip_suffix(call):
    return re.sub(r'/[A-Z0-9]+$', '', call)


def parse_rbn(path, band=None, start=None, end=None):
    """Parse RBN CSV, optionally filtering by band and time window."""
    spots = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if band and row.get('band') != band:
                continue
            if row.get('tx_mode', '') != 'CW':
                continue
            ts = row.get('date', '')
            if start or end:
                time_part = ts.split(' ')[1] if ' ' in ts else ''
                if start and time_part < start:
                    continue
                if end and time_part >= end:
                    continue
            spots.append({
                'skimmer': row.get('callsign', ''),
                'call': row.get('dx', ''),
                'freq': float(row.get('freq', 0)),
                'db': int(row.get('db', 0)),
                'time': ts,
                'speed': int(row.get('speed', 0)),
            })
    return spots


def main():
    ap = argparse.ArgumentParser(description='Compare SparkGap vs RBN')
    ap.add_argument('our_spots', help='Our spot log file')
    ap.add_argument('rbn_csv', help='RBN daily CSV file')
    ap.add_argument('--start', default=None, help='Start time HH:MM (UTC)')
    ap.add_argument('--end', default=None, help='End time HH:MM (UTC)')
    ap.add_argument('--band', default='20m', help='Band filter for RBN (default: 20m)')
    ap.add_argument('--freq-min', type=float, default=None, help='Min freq kHz')
    ap.add_argument('--freq-max', type=float, default=None, help='Max freq kHz')
    args = ap.parse_args()

    start_filter = f"{args.start}:00" if args.start else None
    end_filter = f"{args.end}:00" if args.end else None

    our_spots = parse_our_spots(args.our_spots)
    rbn_spots = parse_rbn(args.rbn_csv, band=args.band, start=start_filter, end=end_filter)

    if args.freq_min:
        rbn_spots = [s for s in rbn_spots if s['freq'] >= args.freq_min]
    if args.freq_max:
        rbn_spots = [s for s in rbn_spots if s['freq'] <= args.freq_max]

    our_calls = set(strip_suffix(s['call']) for s in our_spots)
    rbn_calls = set(strip_suffix(s['call']) for s in rbn_spots)
    rbn_skimmers = set(s['skimmer'] for s in rbn_spots)

    rbn_call_skimmer_count = defaultdict(set)
    for s in rbn_spots:
        rbn_call_skimmer_count[strip_suffix(s['call'])].add(s['skimmer'])

    matched = our_calls & rbn_calls
    we_missed = rbn_calls - our_calls
    we_extra = our_calls - rbn_calls

    widely_spotted = {c for c, sks in rbn_call_skimmer_count.items() if len(sks) >= 5}
    missed_widely = we_missed & widely_spotted

    print(f"=== SparkGap vs RBN Comparison ===")
    print(f"Our spots:    {len(our_spots)} total, {len(our_calls)} unique calls")
    print(f"RBN spots:    {len(rbn_spots)} total, {len(rbn_calls)} unique calls")
    print(f"RBN skimmers: {len(rbn_skimmers)} reporting")
    print()
    print(f"Matched:      {len(matched)} / {len(rbn_calls)}  ({100*len(matched)//len(rbn_calls) if rbn_calls else 0}% recall)")
    print(f"We missed:    {len(we_missed)}")
    print(f"Our extras:   {len(we_extra)} (not in RBN)")
    print()

    if missed_widely:
        print(f"--- Missed calls spotted by 5+ RBN skimmers ({len(missed_widely)}) ---")
        for call in sorted(missed_widely):
            n = len(rbn_call_skimmer_count[call])
            print(f"  {call:12s}  (spotted by {n} skimmers)")
        print()

    if we_missed:
        by_count = sorted(we_missed, key=lambda c: -len(rbn_call_skimmer_count[c]))
        print(f"--- All missed calls (sorted by RBN skimmer count) ---")
        for call in by_count[:50]:
            n = len(rbn_call_skimmer_count[call])
            print(f"  {call:12s}  ({n} skimmers)")
        if len(we_missed) > 50:
            print(f"  ... and {len(we_missed) - 50} more")
        print()

    if we_extra:
        print(f"--- Our extras not in RBN (sample, {len(we_extra)} total) ---")
        for call in sorted(we_extra)[:30]:
            print(f"  {call}")
        if len(we_extra) > 30:
            print(f"  ... and {len(we_extra) - 30} more")


if __name__ == '__main__':
    main()
