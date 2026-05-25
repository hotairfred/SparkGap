#!/usr/bin/env python3
"""Multi-recording B1 benchmark harness.

Runs sparkgap file mode against B1_seg{1,2,3,4}.wav (or any specified
subset), captures the DECODED CALLSIGNS section from each run, and
emits a consolidated report.

For segments that have a sibling `<base>_cq_key_*.txt` answer key, scores
recall (strict + slash-tolerant) using the same matching rule as
`tools/eval/score_b1_seg2.py`.  For segments without a key, reports
spot count + SCP-validation % + cross-segment overlap.

USAGE
-----
  tools/eval/b1_benchmark.py [recording.wav ...]                 # run, score, report
  tools/eval/b1_benchmark.py --score-only logA.log logB.log ...  # skip runs, score existing logs
  tools/eval/b1_benchmark.py --dry-run                           # show what would run, no work

Default recording set: all B1_seg*.wav found under DEFAULT_RECDIR.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path


DEFAULT_RECDIR  = '/mnt/atlas/skimmer/recordings'
DEFAULT_CONFIG  = 'sk_5band.json'
SCP_PATH        = 'MASTER.SCP'
LOG_DIR_DEFAULT = '/tmp/b1_benchmark'


# ----------------------------------------------------------------------------
# Scoring helpers (slash-tolerant rule lives in tools/eval/score_b1_seg2.py;
# keep them in sync.)

def load_callset(path):
    with open(path) as f:
        return set(c.strip().upper()
                   for c in f.read().replace('\n', ',').split(',')
                   if c.strip())


def slash_variants(call):
    out = {call}
    if '/' in call:
        for p in call.split('/'):
            if p:
                out.add(p)
    return out


def slash_tolerant_match(spots, key_call):
    cands = slash_variants(key_call)
    for spot in spots:
        if spot in cands or any(v in cands for v in slash_variants(spot)):
            return True
    return False


def parse_decoded_calls(log_path):
    """Extract calls from the DECODED CALLSIGNS section at the end of the log."""
    spots = set()
    in_block = False
    line_re = re.compile(r'\d+\.\d\s+kHz\s+(\S+)\s+\d+\s+dB')
    with open(log_path) as f:
        for ln in f:
            if 'DECODED CALLSIGNS' in ln:
                in_block = True
                continue
            if in_block:
                m = line_re.search(ln)
                if m:
                    spots.add(m.group(1).upper())
                elif ln.strip() == '':
                    in_block = False
    return spots


# ----------------------------------------------------------------------------
# Run + score one recording

def find_cq_key(wav_path):
    """Look for a sibling <stem>_cq_key*.txt next to the recording."""
    p = Path(wav_path)
    base = p.stem
    if '_' in base:
        base = base.split('_')[0] + '_' + base.split('_')[1]  # e.g. B1_seg1
    parent = p.parent
    for cand in sorted(parent.glob(f'{base}_cq_key*.txt')):
        return cand
    return None


def run_recording(wav_path, log_dir, config, dry_run=False):
    """Invoke sparkgap.py against one recording.  Returns log path on success."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(wav_path).stem
    log_path = log_dir / f'{stem}.log'
    cmd = ['python3', 'sparkgap.py',
           '--file', str(wav_path),
           '--start-min', '0', '--end-min', '15',
           '--config', config]
    print(f'== run {stem} ==')
    print('  cmd:', ' '.join(cmd))
    print('  log:', log_path)
    if dry_run:
        print('  (dry-run, skipping)')
        return log_path
    t0 = time.time()
    with open(log_path, 'wb') as f:
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    dt = time.time() - t0
    if proc.returncode != 0:
        print(f'  FAILED (rc={proc.returncode}) after {dt:.0f}s')
        return None
    print(f'  done in {dt:.0f}s')
    return log_path


# ----------------------------------------------------------------------------
# Report

def print_report(results, scp=None):
    """results: list of dicts with keys: name, log, spots, key_calls (or None), strict, tolerant"""
    print()
    print('=' * 70)
    print('B1 multi-recording benchmark report')
    print('=' * 70)

    has_keys = any(r.get('key_calls') for r in results)
    if has_keys:
        print(f'{"recording":<35} {"spots":>5} {"strict":>10} {"tolerant":>10}')
    else:
        print(f'{"recording":<35} {"spots":>5} {"scp_ok":>7} {"unique":>7}')
    print('-' * 70)

    union_spots = set()
    per_seg_spots = {}
    for r in results:
        union_spots |= r['spots']
        per_seg_spots[r['name']] = r['spots']

    for r in results:
        scp_ok = len(r['spots'] & scp) if scp else 0
        unique_to_this = r['spots'] - set().union(
            *(per_seg_spots[n] for n in per_seg_spots if n != r['name']))
        if r.get('key_calls'):
            n_key = len(r['key_calls'])
            print(f"{r['name']:<35} {len(r['spots']):>5} "
                  f"{r['strict']:>4}/{n_key:<3} "
                  f"{r['tolerant']:>4}/{n_key:<3}")
        else:
            print(f"{r['name']:<35} {len(r['spots']):>5} "
                  f"{scp_ok:>7} {len(unique_to_this):>7}")

    # Cross-segment overlap analysis
    if len(results) >= 2:
        print()
        print('Cross-segment call overlap (calls appearing in N of the N segments):')
        call_in = defaultdict(int)
        for r in results:
            for c in r['spots']:
                call_in[c] += 1
        for k in range(len(results), 0, -1):
            calls = [c for c, n in call_in.items() if n == k]
            print(f'  in {k}/{len(results)} segments: {len(calls):>4} calls'
                  + (f' (e.g. {", ".join(sorted(calls)[:5])})' if calls and k > 1 else ''))


# ----------------------------------------------------------------------------
# Main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('recordings', nargs='*',
                    help='Recordings to benchmark.  Default: all B1_seg*.wav '
                    f'under {DEFAULT_RECDIR}.')
    ap.add_argument('--config', default=DEFAULT_CONFIG)
    ap.add_argument('--log-dir', default=LOG_DIR_DEFAULT, type=Path)
    ap.add_argument('--score-only', action='store_true',
                    help='Recordings argument is treated as existing log paths; '
                    'do not re-run sparkgap.')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not args.recordings:
        recdir = Path(DEFAULT_RECDIR)
        args.recordings = sorted(str(p) for p in recdir.glob('B1_seg*.wav'))
        if not args.recordings:
            print(f'No B1_seg*.wav found under {recdir}', file=sys.stderr)
            sys.exit(1)

    try:
        scp = load_callset(SCP_PATH)
    except FileNotFoundError:
        scp = None

    results = []
    for rec in args.recordings:
        rec_path = Path(rec)
        if args.score_only:
            log_path = rec_path
            stem = log_path.stem
            # Infer the wav stem to look up the key
            wav_candidate = Path(DEFAULT_RECDIR) / f'{stem}.wav'
        else:
            log_path = run_recording(rec_path, args.log_dir, args.config,
                                     dry_run=args.dry_run)
            wav_candidate = rec_path
            stem = rec_path.stem
            if log_path is None:
                continue
            if args.dry_run:
                continue
        spots = parse_decoded_calls(log_path)
        key = find_cq_key(wav_candidate)
        if key:
            key_calls = load_callset(key)
            strict = len(spots & key_calls)
            tol = sum(1 for k in key_calls if slash_tolerant_match(spots, k))
        else:
            key_calls = None
            strict = tol = 0
        results.append({
            'name':      stem,
            'log':       log_path,
            'spots':     spots,
            'key_calls': key_calls,
            'strict':    strict,
            'tolerant':  tol,
        })

    if args.dry_run:
        return
    print_report(results, scp=scp)


if __name__ == '__main__':
    main()
