#!/usr/bin/env python3
"""RBN-validated scoring for OpenSkimmer vs CW Skimmer.

Builds a ground truth from the union of CW Skimmer spots + our spots,
validated against RBN network data. Scores both decoders fairly.

Usage:
    # Score file mode output against B1 seg2 (40m CWT, Mar 19 0300Z)
    python3 eval_rbn_validated.py <our_spot_log> [--rbn rbn_data/20260319.csv]

    # Or just score the default recording
    python3 eval_rbn_validated.py /tmp/filemode_test.log
"""
import argparse
import csv
import re
import sys
from pathlib import Path


CWSKIMMER_GOLD = set(
    "AA3B,AA4NP,AI5IN,CY0S,DF7TV,EB1EOE,HZ1TT,I1MMR,IK4QJF,K1BZ,K1DW,"
    "K1GU,K1HZ,K2LE,K4IU,K5DXR,K5YCM,K8WWS,K9MA,KB2BK,KB4EKK,KD0RC,"
    "KD4JG,KI7MD,KM0O,KM9R,KV0I,KW7Q,N2EY,N3AD,N5AW,N5NA,N7DEY,ND9M,"
    "NQ5P,NT6Q,NY6C,PA3AAV,PY2NA,R6JY,VE3KIU,VE6JF,VE7WO,VE7ZO,W0EAS,"
    "W0TG,W1QK,W2NMI,W3US,W4CMG,W4IT,W4SPR,W5JMW,W5TM,W6AJR,W6IWI,"
    "W7JET,W8EH,W9CF,W9ILY,WA0I,WA5RML,WB0OQV,WB2AA,WR7T,WU6P,ZA1EM"
    .split(",")
)

DEFAULT_RBN = "rbn_data/20260319.csv"
RBN_BAND = "40m"
RBN_HOUR = " 03:"


def load_rbn_calls(csv_path, band=RBN_BAND, hour=RBN_HOUR):
    calls = set()
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            if row['band'] == band and row['tx_mode'] == 'CW' and hour in row['date']:
                calls.add(row['dx'])
    return calls


def load_scp(path="MASTER.SCP"):
    calls = set()
    with open(path) as f:
        for line in f:
            c = line.strip().replace('\r', '').upper()
            if c and not c.startswith('#'):
                calls.add(c)
    return calls


def parse_spots(path):
    calls = set()
    pat = re.compile(r'SPOT.*?(\d+\.\d+)\s+kHz\s+(\S+)\s+(\d+)\s+dB\s+(\d+)\s+WPM')
    pat2 = re.compile(r'SPOT:\s+([\d.]+)\s+kHz\s+(\S+)\s+(\d+)\s+dB\s+(\d+)\s+WPM')
    with open(path) as f:
        for line in f:
            m = pat.search(line) or pat2.search(line)
            if m:
                call = re.sub(r'/.*', '', m.group(2))
                if len(call) >= 3:
                    calls.add(call)
    return calls


def main():
    ap = argparse.ArgumentParser(description='RBN-validated scoring')
    ap.add_argument('spot_log', help='Our spot log file')
    ap.add_argument('--rbn', default=DEFAULT_RBN, help='RBN daily CSV')
    ap.add_argument('--scp', default='MASTER.SCP', help='SCP database')
    args = ap.parse_args()

    rbn = load_rbn_calls(args.rbn)
    scp = load_scp(args.scp)
    ours = parse_spots(args.spot_log)
    cwskim = CWSKIMMER_GOLD

    # Validated truth: calls spotted by either decoder AND confirmed by RBN or the other decoder
    cwskim_confirmed = cwskim & rbn          # CW Skimmer calls also in RBN
    ours_confirmed = ours & rbn              # Our calls also in RBN
    mutual = cwskim & ours                   # Both decoders agree (no RBN needed)
    truth = cwskim_confirmed | ours_confirmed | mutual

    # Scores
    our_hits = ours & truth
    cwskim_hits = cwskim & truth

    we_beat_cwskim = (our_hits - cwskim_hits)   # We found, they didn't
    cwskim_beats_us = (cwskim_hits - our_hits)  # They found, we didn't
    both_found = our_hits & cwskim_hits         # Both found

    # Unverified (no RBN, no mutual confirmation)
    our_unverified = ours - truth - cwskim
    cwskim_unverified = cwskim - truth - ours

    print(f"=== RBN-Validated Scoring ===")
    print(f"RBN 40m CW 0300-0400Z: {len(rbn)} unique calls")
    print(f"CW Skimmer raw:        {len(cwskim)} calls ({len(cwskim & rbn)} RBN-confirmed)")
    print(f"OpenSkimmer raw:       {len(ours)} calls ({len(ours & rbn)} RBN-confirmed)")
    print(f"Validated truth:       {len(truth)} calls")
    print()
    print(f"{'Metric':<30s} {'OpenSkimmer':>12s} {'CW Skimmer':>12s}")
    print(f"{'-'*30} {'-'*12} {'-'*12}")
    print(f"{'Score vs truth':<30s} {f'{len(our_hits)}/{len(truth)}':>12s} {f'{len(cwskim_hits)}/{len(truth)}':>12s}")
    print(f"{'Recall':<30s} {f'{100*len(our_hits)/len(truth):.0f}%':>12s} {f'{100*len(cwskim_hits)/len(truth):.0f}%':>12s}")
    print(f"{'Exclusive wins':<30s} {len(we_beat_cwskim):>12d} {len(cwskim_beats_us):>12d}")
    print(f"{'Both found':<30s} {len(both_found):>12d} {len(both_found):>12d}")
    print(f"{'Unverified':<30s} {len(our_unverified):>12d} {len(cwskim_unverified):>12d}")
    print()

    if cwskim_beats_us:
        print(f"--- CW Skimmer beats us ({len(cwskim_beats_us)} calls) ---")
        for call in sorted(cwskim_beats_us):
            in_rbn = "RBN" if call in rbn else "no-RBN"
            in_scp = "SCP" if call in scp else "no-SCP"
            print(f"  {call:12s}  [{in_rbn}, {in_scp}]")
        print()

    if we_beat_cwskim:
        print(f"--- We beat CW Skimmer ({len(we_beat_cwskim)} calls) ---")
        for call in sorted(we_beat_cwskim):
            in_rbn = "RBN" if call in rbn else "no-RBN"
            print(f"  {call:12s}  [{in_rbn}]")
        print()

    if our_unverified:
        print(f"--- Our unverified ({len(our_unverified)} calls) ---")
        in_scp_count = len(our_unverified & scp)
        print(f"  {in_scp_count}/{len(our_unverified)} in SCP")
        for call in sorted(our_unverified):
            tag = "SCP" if call in scp else "NOT-SCP"
            print(f"  {call:12s}  [{tag}]")


if __name__ == '__main__':
    main()
