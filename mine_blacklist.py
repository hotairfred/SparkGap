#!/usr/bin/env python3
"""mine_blacklist.py — regenerate blacklist.txt candidates from 3-way logs.

Per feedback_blacklist_mining.md: blacklist.txt is methodology-driven,
NOT hand-curated. Regenerate periodically rather than editing.

Inputs:
  /tmp/os_stream.log   — our spots (sg_tee)
  /tmp/sdc_stream.log  — SDC tee
  /tmp/rbn_stream.log  — worldwide RBN tee

A CW call qualifies for the high-confidence list iff:
  - We emitted it ≥3 times across ≥2 distinct hours
  - Neither SDC tee nor worldwide-RBN tee ever heard it (in window)
  - Matches 1×1 pattern (@#@) — single letter, single digit, single letter

Suspect tier (4+ char calls with same emit/no-peer pattern) is saved
to blacklist_candidates_suspect.txt for human review, NOT auto-applied.

Run:
  python3 mine_blacklist.py
  python3 mine_blacklist.py --since 00:00 --until 23:59
  python3 mine_blacklist.py --apply        # write candidates into blacklist.txt
"""
import argparse
import os
import re
import shutil
import time
from collections import defaultdict
from datetime import datetime, timezone


SPOT_RE = re.compile(
    r'^(\d{2}:\d{2}:\d{2}) DX de (\S+?):?\s+([\d.]+)\s+(\S+)\s+(\S+)'
)
ONExONE_RE = re.compile(r'^[A-Z][0-9][A-Z]$')


def parse_log(path, only_cw=True, t_start=None, t_end=None):
    """Returns (emission_count_dict, hour_set_dict) per call."""
    counts = defaultdict(int)
    hours = defaultdict(set)
    if not os.path.exists(path):
        return counts, hours
    with open(path, errors='replace') as f:
        for line in f:
            m = SPOT_RE.match(line)
            if not m:
                continue
            ts, sp, freq, call, mode = m.groups()
            if only_cw and mode != 'CW':
                continue
            if t_start and ts < t_start:
                continue
            if t_end and ts > t_end:
                continue
            counts[call] += 1
            hours[call].add(ts[:2])
    return counts, hours


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--our', default='/tmp/os_stream.log')
    p.add_argument('--sdc', default='/tmp/sdc_stream.log')
    p.add_argument('--rbn', default='/tmp/rbn_stream.log')
    p.add_argument('--since', default=None, help='HH:MM:SS lower bound')
    p.add_argument('--until', default=None, help='HH:MM:SS upper bound')
    p.add_argument('--min-emit', type=int, default=3)
    p.add_argument('--min-hours', type=int, default=2)
    p.add_argument('--apply', action='store_true',
                   help='Regenerate blacklist.txt with the new high-conf set')
    p.add_argument('--out-high', default='blacklist_candidates_high_conf.txt')
    p.add_argument('--out-suspect', default='blacklist_candidates_suspect.txt')
    args = p.parse_args()

    our_count, our_hours = parse_log(args.our, t_start=args.since, t_end=args.until)
    sdc_count, _ = parse_log(args.sdc, t_start=args.since, t_end=args.until)
    rbn_count, _ = parse_log(args.rbn, t_start=args.since, t_end=args.until)

    # Exclude our own callsign signatures from the candidate pool — they
    # appear in our log because of self-spot loops we'd rather not blacklist
    # if Graham (or anyone) IS legitimately heard later.
    own_calls = {'WF8Z', 'WF8Z-1', 'WF8Z-#'}

    high_conf = []
    suspect = []
    for call, n in our_count.items():
        if call in own_calls:
            continue
        if call in sdc_count or call in rbn_count:
            continue  # peer-corroborated, not noise
        h = len(our_hours[call])
        if n < args.min_emit or h < args.min_hours:
            continue
        if ONExONE_RE.match(call):
            high_conf.append((call, n, h))
        elif len(call) >= 4:
            suspect.append((call, n, h))

    high_conf.sort(key=lambda x: -x[1])
    suspect.sort(key=lambda x: -x[1])

    # Print summary
    print(f'Our log: {sum(our_count.values())} CW spots, {len(our_count)} unique calls')
    print(f'SDC tee: {sum(sdc_count.values())} spots')
    print(f'RBN tee: {sum(rbn_count.values())} spots')
    print()
    print(f'=== High-confidence 1x1 candidates: {len(high_conf)} ===')
    for call, n, h in high_conf[:80]:
        print(f'  {call:<6} {n:>4} emissions across {h} hours')
    print()
    print(f'=== Suspect (4+ char) candidates: {len(suspect)} (top 30) ===')
    for call, n, h in suspect[:30]:
        print(f'  {call:<8} {n:>4} emissions across {h} hours')

    # Write candidate files
    ts_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    with open(args.out_high, 'w') as f:
        f.write('# High-confidence blacklist candidates ... 1x1 calls emitted by us\n')
        f.write(f'# repeatedly, never seen by SDC or worldwide RBN. Generated {ts_utc}\n')
        f.write('# from mine_blacklist.py over current /tmp/{os,sdc,rbn}_stream.log.\n#\n')
        f.write('# Format: one CALL per line. # for comments.\n#\n')
        f.write(f'# call  emissions  hours\n')
        for call, n, h in high_conf:
            f.write(f'{call}    # {n} emissions across {h} hours\n')
    with open(args.out_suspect, 'w') as f:
        f.write('# Suspect blacklist candidates ... 4+ char calls emitted by us\n')
        f.write('# repeatedly, never seen by peers. HUMAN REVIEW ONLY — do not\n')
        f.write(f'# auto-apply. Generated {ts_utc}.\n#\n')
        for call, n, h in suspect:
            f.write(f'{call}    # {n} emissions across {h} hours\n')

    print(f'\nCandidate files written: {args.out_high}, {args.out_suspect}')

    if args.apply:
        # Regenerate blacklist.txt — preserve any "Operator-reported" hand-
        # adds (like G7D from the G3XTZ email), append above the auto-list.
        # This is a small concession to the "no hand edits" rule for cases
        # where an operator complaint comes in faster than the next mining
        # cycle — those entries get a comment block annotating provenance.
        out_path = 'blacklist.txt'
        backup = out_path + '.bak'
        if os.path.exists(out_path):
            shutil.copy(out_path, backup)
        # Read existing operator-reported block to preserve
        operator_block = []
        if os.path.exists(out_path):
            with open(out_path) as f:
                in_op_block = False
                for line in f:
                    if 'Operator-reported' in line:
                        in_op_block = True
                    if in_op_block:
                        operator_block.append(line.rstrip())
                    if in_op_block and line.strip() == '':
                        in_op_block = False
        with open(out_path, 'w') as f:
            f.write('# SparkGap blacklist ... CALLs that should never be emitted.\n')
            f.write('# Format: one CALL per line, # for comments.\n')
            f.write(f'# Generated {ts_utc} from mine_blacklist.py over\n')
            f.write('# /tmp/{os,sdc,rbn}_stream.log via 3-way score-log mining.\n#\n')
            f.write('# All entries below are 1x1 calls our decoder repeatedly emitted\n')
            f.write('# but neither SDC nor worldwide RBN ever heard. Decode noise\n')
            f.write('# hitting random SCP entries by chance.\n#\n')
            f.write(f'# 1x1 noise (high confidence) — {len(high_conf)} entries\n')
            for call, n, h in high_conf:
                f.write(f'{call}\n')
            if operator_block:
                f.write('\n')
                for line in operator_block:
                    f.write(line + '\n')
        print(f'\n{out_path} regenerated. Backup at {backup}.')
        print(f'Restart skimmer to apply.')


if __name__ == '__main__':
    main()
