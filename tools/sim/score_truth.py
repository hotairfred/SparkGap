#!/usr/bin/env python3
"""Score SparkGap spots against a composed scene's exact truth.

Every distinct spotted (call, frequency) is one of:
  RUNNER       a runner, spotted within --tol-khz of its frequency   (correct)
  CALLER       a station that only ever called in this scene         (RBN rules: not a spot)
  WRONG_FREQ   a real station, but more than --tol-khz from where it transmitted
  BUST         a call that no station in the scene sent              (decode error)
and runner recall = runners spotted at their frequency / runners in the scene.

Spots are read like tools/eval/rbn_confirm.py: file-mode "SPOT: 7031.2 kHz CALL",
live "*** SPOT:  7031.2  CALL", or cluster "DX de X:  7031.2  CALL".

usage: score_truth.py LOG SCENE.truth.json [--tol-khz 0.3] [--list]
"""
import argparse
import json
import re
from collections import defaultdict

SPOT_RE = re.compile(r"(?:SPOT:|DX de \S+:)\s+([\d.]+)\s+(?:kHz\s+)?([A-Z0-9/]+)", re.I)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('log')
    ap.add_argument('truth')
    ap.add_argument('--tol-khz', type=float, default=0.3)
    ap.add_argument('--list', action='store_true', help='list calls in each category')
    a = ap.parse_args()
    tr = json.load(open(a.truth))
    tol = a.tol_khz * 1e3
    where = defaultdict(list)                  # call -> [(rf_hz, role)]
    for s in tr['stations']:
        where[s['call'].upper()].append((s['rf_hz'], s['role']))
    runners = {s['call'].upper(): s for s in tr['stations'] if s['role'] == 'runner'}
    spots = set()
    for ln in open(a.log, errors='replace'):
        if 'FT8' in ln:
            continue
        m = SPOT_RE.search(ln)
        if m:
            spots.add((m.group(2).upper(), round(float(m.group(1)) * 1e3, -2)))
    rank = {'RUNNER': 0, 'CALLER': 1, 'WRONG_FREQ': 2, 'BUST': 3}
    best = {}                                  # call -> best category over its spots
    found = set()
    for call, f in spots:
        st = where.get(call)
        if not st:
            k = 'BUST'
        else:
            near = [(rf, role) for rf, role in st if abs(rf - f) <= tol]
            k = ('RUNNER' if any(role == 'runner' for _rf, role in near)
                 else 'CALLER' if near else 'WRONG_FREQ')
        if k == 'RUNNER':
            found.add(call)
        if call not in best or rank[k] < rank[best[call]]:
            best[call] = k
    cats = defaultdict(set)
    for call, k in best.items():
        cats[k].add(call)
    n = sum(len(v) for v in cats.values())
    print(f'{a.log}: {len(spots)} spots ({n} distinct calls) vs {len(runners)} runners, '
          f'{len(where)} stations in the scene')
    print(f'  runner recall   {len(found)}/{len(runners)} ({100.0 * len(found) / max(len(runners), 1):.0f}%)')
    for k in ('RUNNER', 'CALLER', 'WRONG_FREQ', 'BUST'):
        v = cats[k]
        print(f'  {k:<11} {len(v):4d} ({100.0 * len(v) / max(n, 1):3.0f}% of spotted calls)'
              + (':  ' + ' '.join(sorted(v)) if a.list and v else ''))
    if a.list:
        print('  runners missed: ' + ' '.join(sorted(set(runners) - found)))


if __name__ == '__main__':
    main()
