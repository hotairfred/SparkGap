#!/usr/bin/env python3
"""compute_ppm_skew.py — derive frequency calibration offset from logs.

Compares our spot frequencies against worldwide RBN consensus on the
same (call, minute) buckets. Median delta = our actual ppm skew.
That value, written into sk_5band.json's `ppm_offset` field, gets
subtracted from every freq we report — bringing W3RGA's frequency-
skew flag back under ±3 ppm without any hardware change.

Inputs (default paths, override via CLI):
  /tmp/os_stream.log   — sg_tee output of skimmer1's :7300 telnet
  /tmp/rbn_stream.log  — rbn_tee output of worldwide RBN telnet :7000

Method:
  1. Parse both logs into (call, minute) → freq_khz map
  2. For each bucket where ≥2 RBN nodes (excluding us) spotted the call,
     compute median RBN freq.
  3. Compare our median freq for the same bucket; delta_ppm = the gap.
  4. Median across all bucket deltas = our overall skew.

Only CW spots used: FT8 frequencies have audio-offset noise we don't
need to model; CW is the most stable signal type for this analysis.

Run:
  python3 compute_ppm_skew.py
  python3 compute_ppm_skew.py --our /path/to/os.log --rbn /path/to/rbn.log
  python3 compute_ppm_skew.py --since 03:00 --until 23:00
"""
import argparse
import re
import statistics
from collections import defaultdict


SPOT_RE = re.compile(
    r'^(\d{2}:\d{2}:\d{2}) DX de (\S+?):?\s+([\d.]+)\s+(\S+)\s+(\S+)'
)


def parse_log(path, exclude_spotter_prefix=None, t_start=None, t_end=None):
    """Returns dict[(call, 'HH:MM')] -> dict[spotter] -> [freq_khz, ...].

    For CW spots only. exclude_spotter_prefix filters self-spots out of
    the worldwide tee so our own re-broadcast doesn't bias RBN consensus.
    """
    by_bucket = defaultdict(lambda: defaultdict(list))
    with open(path, errors='replace') as f:
        for line in f:
            m = SPOT_RE.match(line)
            if not m:
                continue
            ts, spotter, freq_str, call, mode = m.groups()
            spotter = spotter.rstrip(':')
            if mode != 'CW':
                continue
            if exclude_spotter_prefix and spotter.startswith(exclude_spotter_prefix):
                continue
            if t_start and ts < t_start:
                continue
            if t_end and ts > t_end:
                continue
            try:
                freq = float(freq_str)
            except ValueError:
                continue
            minute = ts[:5]
            by_bucket[(call, minute)][spotter].append(freq)
    return by_bucket


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--our', default='/tmp/os_stream.log',
                   help='Our spot log (sg_tee output)')
    p.add_argument('--rbn', default='/tmp/rbn_stream.log',
                   help='Worldwide RBN tee log')
    p.add_argument('--exclude-prefix', default='WF8Z',
                   help='RBN spotter callsign prefix to exclude as self')
    p.add_argument('--min-spotters', type=int, default=2,
                   help='Min RBN spotters per bucket for consensus')
    p.add_argument('--since', default=None,
                   help='Only buckets at/after HH:MM (default: all)')
    p.add_argument('--until', default=None,
                   help='Only buckets at/before HH:MM (default: all)')
    args = p.parse_args()

    our = parse_log(args.our, t_start=args.since, t_end=args.until)
    rbn = parse_log(args.rbn, exclude_spotter_prefix=args.exclude_prefix,
                    t_start=args.since, t_end=args.until)

    print(f'Our log: {sum(len(s) for s in our.values())} CW spotter-buckets'
          f' across {len(our)} (call,minute) pairs')
    print(f'RBN log: {sum(len(s) for s in rbn.values())} CW spotter-buckets'
          f' across {len(rbn)} (call,minute) pairs')

    deltas_per_band = defaultdict(list)
    deltas_all = []
    for key, our_spotters in our.items():
        # We only have one spotter (us) — flatten our freqs
        our_freqs = [f for freqs in our_spotters.values() for f in freqs]
        if not our_freqs:
            continue
        rbn_spotters = rbn.get(key, {})
        if len(rbn_spotters) < args.min_spotters:
            continue
        our_med = statistics.median(our_freqs)
        # Take per-spotter median first (so a single noisy spotter doesn't
        # dominate), then median across spotters.
        rbn_medians = [statistics.median(freqs) for freqs in rbn_spotters.values()]
        rbn_med = statistics.median(rbn_medians)
        if rbn_med <= 0:
            continue
        delta_ppm = (our_med - rbn_med) / rbn_med * 1e6
        # Filter outliers — anything beyond ±50 ppm is a busted decode,
        # not a calibration signal.
        if abs(delta_ppm) > 50:
            continue
        deltas_all.append(delta_ppm)
        # Bucket by approximate band for per-band analysis
        if rbn_med < 4000:    band = '80m'
        elif rbn_med < 8000:  band = '40m'
        elif rbn_med < 11000: band = '30m'
        elif rbn_med < 15000: band = '20m'
        elif rbn_med < 19000: band = '17m'
        elif rbn_med < 22000: band = '15m'
        elif rbn_med < 25000: band = '12m'
        else:                 band = '10m'
        deltas_per_band[band].append(delta_ppm)

    if not deltas_all:
        print('\nNo matched buckets found. Run sg_tee and rbn_tee for a few hours first.')
        return

    deltas_all.sort()
    n = len(deltas_all)
    overall = statistics.median(deltas_all)
    p25 = deltas_all[n // 4]
    p75 = deltas_all[3 * n // 4]
    print(f'\n=== Per-band skew ===')
    print(f'{"Band":<6s} {"N":>5s}  {"Median":>8s}  {"P25":>8s}  {"P75":>8s}')
    for band in ['80m', '40m', '30m', '20m', '17m', '15m', '12m', '10m']:
        d = sorted(deltas_per_band.get(band, []))
        if not d:
            continue
        nb = len(d)
        med = statistics.median(d)
        b25 = d[nb // 4] if nb >= 4 else d[0]
        b75 = d[3 * nb // 4] if nb >= 4 else d[-1]
        print(f'{band:<6s} {nb:>5d}  {med:>+7.2f}  {b25:>+7.2f}  {b75:>+7.2f}')

    print(f'\n=== Overall ===')
    print(f'Matched buckets: {n}')
    print(f'Median skew:     {overall:+.2f} ppm')
    print(f'P25 / P75:       {p25:+.2f} / {p75:+.2f} ppm')
    print(f'Min / Max:       {min(deltas_all):+.2f} / {max(deltas_all):+.2f} ppm')
    print(f'\nRecommended sk_5band.json `ppm_offset`: {overall:+.2f}')


if __name__ == '__main__':
    main()
