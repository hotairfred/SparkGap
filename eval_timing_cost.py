#!/usr/bin/env python3
"""Validate the ggmorse-inspired timing-cost gate on a recorded WAV.

Goal: prove (or disprove) that timing_cost cleanly separates real decodes
from M5M-class hallucinations. Methodology: scan a frequency range in the
B1_seg2 recording against its known answer key. Every callsign emitted by
decode_channel gets a row in a CSV: call, freq, log_bayes, wpm, cost,
in_key. Then summarise the cost distributions of in-key (true positive)
vs not-in-key (likely false positive) emissions.

If true positives have systematically lower cost than false positives,
the gate signal is real and worth promoting to production.

Usage:
  python3 eval_timing_cost.py
  python3 eval_timing_cost.py --wav /path/to/other.wav --key /path/to/key.txt
"""
import argparse
import sys
import os
import time
import csv
import statistics

# Make itila_cw imports work when run from anywhere
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import itila_cw


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--wav',     default='/mnt/atlas/skimmer/recordings/B1_seg2_15-30min_7090kHz.wav')
    ap.add_argument('--key',     default='/mnt/atlas/skimmer/recordings/B1_seg2_cq_key_56.txt')
    ap.add_argument('--center',  type=float, default=7090.0)
    ap.add_argument('--start',   type=float, default=0.0, help='minutes')
    ap.add_argument('--end',     type=float, default=15.0, help='minutes')
    ap.add_argument('--step',    type=float, default=0.1, help='kHz step')
    ap.add_argument('--band-min',type=float, default=7000.0, help='kHz')
    ap.add_argument('--band-max',type=float, default=7180.0, help='kHz')
    ap.add_argument('--thresh',  type=float, default=10.0, help='log Bayes evidence threshold')
    ap.add_argument('--lpfs',    default='100,200',
                    help='comma-separated LPF cutoffs Hz (default 100,200)')
    ap.add_argument('--out',     default='/tmp/timing_cost_eval.csv')
    args = ap.parse_args()

    with open(args.key) as f:
        gold = {c.strip().upper() for c in f.read().replace('\n', ',').split(',') if c.strip()}
    print(f"Gold key: {len(gold)} calls", flush=True)

    freqs = np.arange(args.band_min, args.band_max, args.step)
    print(f"Scanning {len(freqs)} channels {args.band_min}-{args.band_max} kHz "
          f"over {args.end - args.start:.1f} min", flush=True)

    start_sec = args.start * 60.0
    end_sec   = args.end   * 60.0

    rows = []
    chunk_sec = 120.0
    t = start_sec
    chunk_num = 0

    while t < end_sec:
        t1 = min(t + chunk_sec, end_sec)
        chunk_num += 1
        print(f"\nChunk {chunk_num}: {t/60:.1f}-{t1/60:.1f} min — loading IQ...", flush=True)
        t_load = time.time()
        try:
            iq_cache = itila_cw.load_iq_wav(args.wav, start_sec=t, end_sec=t1)
        except Exception as e:
            print(f"  WAV load error: {e}", flush=True); t = t1; continue
        print(f"  IQ loaded in {time.time()-t_load:.1f}s; scanning {len(freqs)} freqs...", flush=True)

        t_scan = time.time()
        lpfs_list = [int(x) for x in args.lpfs.split(',') if x.strip()]
        for i, freq in enumerate(freqs):
            for lpf in lpfs_list:
                try:
                    env, _ = itila_cw.read_iq_wav(args.wav, args.center, freq,
                                                   iq_cache=iq_cache, lpf_hz=lpf)
                except Exception:
                    continue
                result = itila_cw.decode_channel(env, args.center, freq,
                                                  evidence_threshold=args.thresh,
                                                  verbose=False, lpf_hz=lpf)
                if not result or not result.get('callsigns'):
                    continue
                cost = result.get('timing_cost', 999.0)
                wpm  = result.get('wpm', 0)
                lbf  = result.get('log_bayes', 0)
                for call in result['callsigns']:
                    in_key = call in gold
                    rows.append({
                        'call':       call,
                        'freq':       round(freq, 2),
                        'lpf':        lpf,
                        'wpm':        round(wpm, 1),
                        'log_bayes':  round(lbf, 1),
                        'cost':       round(cost, 3),
                        'in_key':     int(in_key),
                    })
            if (i + 1) % 200 == 0:
                print(f"  ... {i+1}/{len(freqs)} freqs, "
                      f"{len(rows)} emissions, elapsed {time.time()-t_scan:.0f}s", flush=True)
        print(f"  chunk done in {time.time()-t_scan:.0f}s", flush=True)
        t = t1

    # Write CSV
    with open(args.out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['call','freq','lpf','wpm','log_bayes','cost','in_key'])
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {args.out}", flush=True)

    # Summary
    in_key_costs  = [r['cost'] for r in rows if r['in_key'] and r['cost'] < 998]
    off_key_costs = [r['cost'] for r in rows if not r['in_key'] and r['cost'] < 998]

    print(f"\nEmissions:  in-key={sum(r['in_key'] for r in rows)}, "
          f"off-key={sum(1 for r in rows if not r['in_key'])}")
    print(f"Unique calls: in-key={len({r['call'] for r in rows if r['in_key']})}, "
          f"off-key={len({r['call'] for r in rows if not r['in_key']})}")

    def stats(label, xs):
        if not xs:
            print(f"  {label:10s}: n=0  (no data)"); return
        xs = sorted(xs)
        n = len(xs)
        p50 = xs[n//2]
        p90 = xs[min(n-1, int(n*0.9))]
        mean = sum(xs)/n
        print(f"  {label:10s}: n={n:5d}  min={xs[0]:7.3f}  median={p50:7.3f}  "
              f"mean={mean:7.3f}  p90={p90:7.3f}  max={xs[-1]:7.3f}")

    print("\nCost distribution by ground truth:")
    stats('in-key',  in_key_costs)
    stats('off-key', off_key_costs)

    # ROC-style sweep: at cost ≤ T, what fraction of in-key vs off-key get through?
    if in_key_costs and off_key_costs:
        print("\nThreshold sweep (lower cost ⇒ keep):")
        print("  thresh    tp_rate    fp_rate    tp_kept    fp_kept")
        for T in [0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 15.0, 30.0, 50.0, 100.0, 200.0, 999.0]:
            tp = sum(1 for c in in_key_costs  if c <= T)
            fp = sum(1 for c in off_key_costs if c <= T)
            tp_rate = tp / len(in_key_costs)
            fp_rate = fp / len(off_key_costs)
            print(f"  {T:6.1f}    {tp_rate:6.1%}    {fp_rate:6.1%}    {tp:5d}     {fp:5d}")


if __name__ == '__main__':
    main()
