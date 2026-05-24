#!/usr/bin/env python3
"""Slash-tolerant scoring for B1_seg2 file-mode runs against cq_key_56.

Replaces throwaway inline scoring scripts.  Handles the PJ2/AG3I-class
mismatch where cq_key_56 stores the slashed variant but the file-mode
extractor emits only the base call (or vice versa).

Match rule: a cq_key entry matches a spotted call if any of these hold:
  - exact string match
  - cq_key entry has '/', AND any side of the slash equals the spotted call
  - spotted call has '/', AND any side of its slash equals the cq_key entry
  - both have '/' AND either side matches the corresponding side of the other

Usage:
  python3 tools/eval/score_b1_seg2.py /tmp/option_b.log
  python3 tools/eval/score_b1_seg2.py /tmp/option_b.log /tmp/fusion_off.log /tmp/cluster_50.log
"""
import re
import sys
import argparse


KEY_PATH = '/mnt/atlas/skimmer/recordings/B1_seg2_cq_key_56.txt'
SCP_PATH = 'MASTER.SCP'


def load_key(path):
    with open(path) as f:
        return set(c.strip().upper() for c in f.read().replace('\n', ',').split(',') if c.strip())


def load_scp(path):
    s = set()
    with open(path) as f:
        for ln in f:
            ln = ln.strip().upper()
            if ln and not ln.startswith('#') and ln.replace('/', '').isalnum():
                s.add(ln)
    return s


def parse_spots(path):
    spots = set()
    in_decode = False
    for ln in open(path):
        if 'DECODED CALLSIGNS' in ln:
            in_decode = True
            continue
        if in_decode:
            m = re.search(r'\d+\.\d\s+kHz\s+(\S+)\s+\d+\s+dB', ln)
            if m:
                spots.add(m.group(1))
            elif ln.strip() == '':
                in_decode = False
    return spots


def slash_variants(call):
    """Return all string-equivalence candidates for a slashed call.

    PJ2/AG3I -> {'PJ2/AG3I', 'AG3I', 'PJ2'}
    W1AW/0   -> {'W1AW/0', 'W1AW', '0'}  (trailing '0' is harmless garbage)
    W1AW     -> {'W1AW'}
    """
    out = {call}
    if '/' in call:
        for p in call.split('/'):
            if p:
                out.add(p)
    return out


def slash_tolerant_match(spots, key_call):
    """True if any spot equals key_call under slash-tolerant equivalence."""
    candidates = slash_variants(key_call)
    for spot in spots:
        if spot in candidates or any(v in candidates for v in slash_variants(spot)):
            return True, spot
    return False, None


def score(log_path, key, scp=None):
    spots = parse_spots(log_path)
    strict_hits = spots & key
    tolerant_hits = set()
    tolerant_matches = {}  # key_call -> matched spot
    for k in key:
        ok, matched = slash_tolerant_match(spots, k)
        if ok:
            tolerant_hits.add(k)
            if matched != k:
                tolerant_matches[k] = matched
    return spots, strict_hits, tolerant_hits, tolerant_matches


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('logs', nargs='+', help='file-mode log paths to score')
    ap.add_argument('--key', default=KEY_PATH)
    ap.add_argument('--scp', default=SCP_PATH)
    args = ap.parse_args()

    key = load_key(args.key)
    try:
        scp = load_scp(args.scp)
    except FileNotFoundError:
        scp = None

    print(f"{'log':<30s}  {'spots':>6s}  {'strict':>8s}  {'tolerant':>8s}  Δ")
    print('-' * 70)
    for path in args.logs:
        spots, strict, tol, slash_matches = score(path, key, scp)
        label = path.replace('/tmp/', '').replace('.log', '')
        print(f"{label:<30s}  {len(spots):>6d}  {len(strict):>4d}/{len(key):<3d}  {len(tol):>4d}/{len(key):<3d}  +{len(tol)-len(strict)}")
        if slash_matches:
            print(f"  slash-rescued: {sorted(slash_matches.items())}")

    print(f"\nKey calls with slashes: {sorted(k for k in key if '/' in k)}")


if __name__ == '__main__':
    main()
