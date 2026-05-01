#!/usr/bin/env python3
"""score_diff.py — A/B-compare two windows of openskimmer vs SDC vs RBN.

Reads /tmp/{os,sdc,rbn}_stream.log directly (not the hourly rollup, so
window boundaries can be arbitrary HH:MM:SS and don't have to align to
hour edges).

Computes per-window aggregate metrics and prints a side-by-side diff
with deltas. Useful for evaluating what a gate flip actually did vs
what it claimed to do.

Usage:
    python3 score_diff.py 12:00:00 18:00:00  18:00:00 23:59:59
    # → before-window vs after-window comparison

    python3 score_diff.py --label-a baseline --label-b s-floor-on \\
                          12:00 18:00  18:00 23:59
"""
import argparse
import re
import sys
from pathlib import Path

LINE_RE = re.compile(
    r'^(\d{2}:\d{2}:\d{2})\s+DX de \S+:\s+\d+\.\d+\s+([A-Z0-9/]{3,15})\s+(\S+)'
)


def normalise_hms(s):
    """Accept HH:MM or HH:MM:SS; return HH:MM:SS string."""
    parts = s.split(':')
    if len(parts) == 2:
        parts.append('00')
    return ':'.join(p.zfill(2) for p in parts)


def calls_in_window(path, start_hms, end_hms, mode_filter='CW'):
    """Set of unique CW calls in [start, end)."""
    out = set()
    try:
        for line in Path(path).read_text().splitlines():
            m = LINE_RE.match(line)
            if not m:
                continue
            hms, call, mode = m.groups()
            # Wrap-aware window match — handles midnight-crossing windows
            # (end_hms <= start_hms). Without this, `score_diff 23:00 01:00`
            # would return zero because every HH:MM:SS >= '00:00:00'.
            if start_hms <= end_hms:
                in_window = start_hms <= hms < end_hms
            else:
                in_window = hms >= start_hms or hms < end_hms
            if not in_window:
                continue
            if mode_filter and mode.upper() != mode_filter:
                continue
            out.add(call.upper())
    except FileNotFoundError:
        pass
    return out


def score(start_hms, end_hms):
    us  = calls_in_window('/tmp/os_stream.log',  start_hms, end_hms)
    sdc = calls_in_window('/tmp/sdc_stream.log', start_hms, end_hms)
    rbn = calls_in_window('/tmp/rbn_stream.log', start_hms, end_hms)
    hits   = us & (sdc | rbn)
    gold   = us & sdc & rbn
    us_sdc = us & sdc
    solo   = us - sdc - rbn
    return {
        'us':       us,
        'sdc':      sdc,
        'rbn':      rbn,
        'hits':     hits,
        'gold':     gold,
        'us_sdc':   us_sdc,
        'solo':     solo,
        'precision': len(hits) / max(len(us), 1),
        'goldkey':   len(gold) / max(len(us_sdc), 1),
        'recall':    len(us_sdc) / max(len(sdc), 1),
    }


def fmt_pct(x):
    return f'{x*100:5.1f}%'


def fmt_delta_pct(a, b):
    """Format the b-a difference in percentage-points."""
    d = (b - a) * 100
    sign = '+' if d >= 0 else ''
    return f'{sign}{d:.1f}pp'


def fmt_delta_int(a, b):
    d = b - a
    sign = '+' if d >= 0 else ''
    return f'{sign}{d}'


def print_table(label_a, ra, label_b, rb):
    rows = [
        ('us emitted',        len(ra['us']),     len(rb['us'])),
        ('SDC unique',        len(ra['sdc']),    len(rb['sdc'])),
        ('RBN unique',        len(ra['rbn']),    len(rb['rbn'])),
        ('us ∩ SDC',          len(ra['us_sdc']), len(rb['us_sdc'])),
        ('hits (us ∩ peer)',  len(ra['hits']),   len(rb['hits'])),
        ('goldkey hits',      len(ra['gold']),   len(rb['gold'])),
        ('solo (suspect)',    len(ra['solo']),   len(rb['solo'])),
    ]
    print(f"\n{'Metric':<22} {label_a:>14} {label_b:>14} {'Δ':>10}")
    print('-' * 64)
    for name, a, b in rows:
        print(f'{name:<22} {a:>14} {b:>14} {fmt_delta_int(a, b):>10}')
    print('-' * 64)
    print(f"{'precision':<22} {fmt_pct(ra['precision']):>14} {fmt_pct(rb['precision']):>14} {fmt_delta_pct(ra['precision'], rb['precision']):>10}")
    print(f"{'goldkey':<22} {fmt_pct(ra['goldkey']):>14} {fmt_pct(rb['goldkey']):>14} {fmt_delta_pct(ra['goldkey'], rb['goldkey']):>10}")
    print(f"{'recall vs SDC':<22} {fmt_pct(ra['recall']):>14} {fmt_pct(rb['recall']):>14} {fmt_delta_pct(ra['recall'], rb['recall']):>10}")


def print_movers(ra, rb):
    """Calls that moved between solo and hit categories (and vice versa)."""
    a_solo, a_hits = ra['solo'], ra['hits']
    b_solo, b_hits = rb['solo'], rb['hits']
    promoted = (a_solo & b_hits)        # solo in A → hit in B (good)
    demoted  = (a_hits & b_solo)        # hit in A → solo in B (bad)
    new_solo = b_solo - ra['us']        # didn't see in A at all
    new_hit  = b_hits - ra['us']        # didn't see in A at all

    if promoted:
        print(f"\nPromoted (solo → hit), {len(promoted)} calls:")
        print('  ' + ' '.join(sorted(promoted)))
    if demoted:
        print(f"\nDemoted (hit → solo), {len(demoted)} calls — investigate:")
        print('  ' + ' '.join(sorted(demoted)))
    if new_solo:
        print(f"\nNew solo in B, {len(new_solo)} calls:")
        print('  ' + ' '.join(sorted(new_solo)[:30]) + (' ...' if len(new_solo) > 30 else ''))


def main():
    p = argparse.ArgumentParser(description='A/B-compare two score windows')
    p.add_argument('start_a', help='Window A start HH:MM[:SS]')
    p.add_argument('end_a',   help='Window A end HH:MM[:SS]')
    p.add_argument('start_b', help='Window B start HH:MM[:SS]')
    p.add_argument('end_b',   help='Window B end HH:MM[:SS]')
    p.add_argument('--label-a', default='A', help='Window A label')
    p.add_argument('--label-b', default='B', help='Window B label')
    p.add_argument('--movers', action='store_true',
                   help='Also list calls that moved between categories')
    args = p.parse_args()

    sa, ea = normalise_hms(args.start_a), normalise_hms(args.end_a)
    sb, eb = normalise_hms(args.start_b), normalise_hms(args.end_b)
    print(f'Window A ({args.label_a}): {sa} → {ea}')
    print(f'Window B ({args.label_b}): {sb} → {eb}')

    ra = score(sa, ea)
    rb = score(sb, eb)
    print_table(args.label_a, ra, args.label_b, rb)

    if args.movers:
        print_movers(ra, rb)


if __name__ == '__main__':
    main()
