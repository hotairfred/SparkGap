#!/usr/bin/env python3
"""A dense CW band for capacity tests (CQ WW class): N runner cells (cellgen.py, CWT flow), each with its
own pool of callers so no call is on the band twice, placed at random LO..HI kHz at least SPACING Hz
apart, at levels -70 to -45 dBFS over a -50 dBFS noise floor.  The composer keeps every cell of a scene
in memory (400 cells would need about 26 GB), so cells are composed in groups of GROUP and the groups'
I/Q summed; the groups' truth is merged into one.

usage: gen_dense.py OUTDIR N [--minutes 10] [--lo 7000] [--hi 7080] [--centre 7030] [--spacing 100]
                    [--group 50] [--procs 8] [--compose-procs 4] [--seed 2026]
writes OUTDIR/dense_<centre>kHz_192000_24bit.wav and OUTDIR/dense.truth.json (score_truth.py)

The 10-23 capacity notes (hotairfred/SparkGap#47) used 400 runners over 7000-7080 kHz, 10 minutes:
about 9,400 stations, half the runners under 150 Hz from a neighbour (denser than a real CQ WW 40 m).
Each compose process holds about 3 GB at 50 cells.
"""
import argparse
import json
import os
import random
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
POOL = 20               # callers per runner
TONE_KHZ = 0.6          # cellgen's runner pitch: the cell's dial sits this far below the runner


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit('%s failed:\n%s' % (os.path.basename(cmd[1]), r.stderr[-800:]))
    return r


def read24(w, n):
    raw = np.frombuffer(w.readframes(n), np.uint8)
    if not len(raw):
        return None
    b = raw.reshape(-1, 3).astype(np.int32)
    v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
    return np.where(v >= 1 << 23, v - (1 << 24), v).astype(np.int64)


def main():
    ap = argparse.ArgumentParser(usage=argparse.SUPPRESS, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('out')
    ap.add_argument('n', type=int)
    ap.add_argument('--minutes', type=float, default=10)
    ap.add_argument('--lo', type=float, default=7000)
    ap.add_argument('--hi', type=float, default=7080)
    ap.add_argument('--centre', type=float, default=7030)
    ap.add_argument('--spacing', type=float, default=100, help='Hz between runners, at least')
    ap.add_argument('--group', type=int, default=50)
    ap.add_argument('--procs', type=int, default=8, help='cellgen processes')
    ap.add_argument('--compose-procs', type=int, default=4)
    ap.add_argument('--seed', type=int, default=2026)
    a = ap.parse_args()
    if a.n * a.spacing / 1000.0 > (a.hi - a.lo) * 0.8:
        sys.exit('%d runners %g Hz apart do not fit in %g-%g kHz' % (a.n, a.spacing, a.lo, a.hi))
    rng = random.Random(a.seed)
    cells_dir = os.path.join(a.out, 'cells')
    os.makedirs(cells_dir, exist_ok=True)

    calls = [c.strip() for c in open(os.path.join(ROOT, 'MASTER.SCP')) if c.strip() and not c.startswith('#')]
    calls = [c for c in calls if '/' not in c and len(c) <= 6]
    rng.shuffle(calls)
    if len(calls) < a.n * (POOL + 1):
        sys.exit('MASTER.SCP has %d usable calls, %d needed' % (len(calls), a.n * (POOL + 1)))
    runners, rest = calls[:a.n], calls[a.n:]
    jobs = []
    for i, r in enumerate(runners):
        pf = os.path.join(cells_dir, 'pool_%03d.txt' % i)
        with open(pf, 'w') as f:
            f.write('\n'.join(rest[i * POOL:(i + 1) * POOL]) + '\n')
        cmd = [sys.executable, os.path.join(HERE, 'cellgen.py'), os.path.join(cells_dir, 'c%03d' % i),
               '--runner', r, '--minutes', str(a.minutes), '--seed', str(a.seed + i),
               '--wpm', str(int(rng.triangular(20, 40, 28))), '--calls', pf]
        if rng.random() < 0.3:
            cmd.append('--qsb')
        jobs.append(cmd)
    with ThreadPoolExecutor(a.procs) as ex:
        list(ex.map(run, jobs))
    print('%d cells' % len(jobs), flush=True)

    pos = []
    while len(pos) < a.n:
        p = rng.uniform(a.lo, a.hi)
        if all(abs(p - q) >= a.spacing / 1000.0 for q in pos):
            pos.append(p)
    cells = [dict(base='cells/c%03d' % i, rf_khz=round(p - TONE_KHZ, 3), level_dbfs=round(rng.uniform(-70, -45), 1))
             for i, p in enumerate(pos)]
    centre = int(a.centre)
    parts = []
    for g in range(0, len(cells), a.group):
        k = g // a.group
        scene = dict(center_khz=a.centre, rate=192000, duration_s=a.minutes * 60, seed=a.seed + k,
                     noise_dbfs=-50 if k == 0 else None, cells=cells[g:g + a.group])   # the noise once
        sp = os.path.join(a.out, 'group%02d.json' % k)
        with open(sp, 'w') as f:
            json.dump(scene, f, indent=1)
        parts.append((sp, os.path.join(a.out, 'group%02d' % k), g))
    with ThreadPoolExecutor(a.compose_procs) as ex:
        list(ex.map(lambda p: run([sys.executable, os.path.join(HERE, 'compose.py'), p[0], p[1]]), parts))

    out_wav = os.path.join(a.out, 'dense_%dkHz_192000_24bit.wav' % centre)
    ws = [wave.open('%s_%dkHz_192000_24bit.wav' % (base, centre)) for _, base, _ in parts]
    o = wave.open(out_wav, 'wb')
    o.setnchannels(2)
    o.setsampwidth(3)
    o.setframerate(192000)
    clipped = 0
    while True:
        acc = None
        for w in ws:
            v = read24(w, 1 << 20)
            if v is not None:
                acc = v if acc is None else acc + v
        if acc is None:
            break
        clipped += int(np.sum(np.abs(acc) > (1 << 23) - 1))
        acc = np.clip(acc, -(1 << 23), (1 << 23) - 1).astype(np.int32)
        b = np.empty((len(acc), 3), np.uint8)
        b[:, 0], b[:, 1], b[:, 2] = acc & 0xFF, (acc >> 8) & 0xFF, (acc >> 16) & 0xFF
        o.writeframes(b.tobytes())
    o.close()
    truth = None
    for _, base, first in parts:
        with open(base + '.truth.json') as f:
            t = json.load(f)
        for item in t['stations'] + t['messages']:
            item['cell'] += first
        if truth is None:
            truth = t
        else:
            truth['stations'] += t['stations']
            truth['messages'] += t['messages']
        os.remove('%s_%dkHz_192000_24bit.wav' % (base, centre))
    with open(os.path.join(a.out, 'dense.truth.json'), 'w') as f:
        json.dump(truth, f)
    print('%s: %d runners, %d stations, %d samples clipped' % (out_wav, len(cells), len(truth['stations']), clipped))


if __name__ == '__main__':
    main()
