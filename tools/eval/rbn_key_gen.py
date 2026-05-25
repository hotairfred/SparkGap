#!/usr/bin/env python3
"""Generate cq_key files from RBN historical CSV for recording segments.

For a recording file like B1_seg2_15-30min_7090kHz.wav and a parent
timestamp of 2026-03-19 03:00:00 UTC, derive the set of callsigns RBN
saw CQing on 40m during minutes 15-30 (= 03:15:00 — 03:30:00 UTC).

Apply VE7CC's 2+ skimmer consensus rule by default to keep the answer
key high-confidence (a single skimmer hearing a call could be a false
positive; two+ independent skimmers seeing the same call on the same
freq within the window is strong evidence the station was real).

USAGE
-----
  rbn_key_gen.py                         # process all B1_seg*.wav with default parent
  rbn_key_gen.py B1_seg1_*.wav           # process specific files
  rbn_key_gen.py --no-consensus          # disable 2-skimmer filter
  rbn_key_gen.py --csv-zip path.zip      # alternate source CSV
"""
import argparse
import csv
import io
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_CSV_ZIP = '/mnt/atlas/skimmer/20260319.zip'
DEFAULT_CSV_NAME = '20260319.csv'
DEFAULT_RECDIR   = '/mnt/atlas/skimmer/recordings'
KEYS_OUT_DIR     = Path('tools/eval/keys')

# Recording-set-to-parent-start-time mapping.  We can't reliably derive
# the start time from the segment filename alone (B1_seg2_15-30min_*.wav
# doesn't say which day or hour) so we hardcode the known parent
# recording.  Filename in the recordings dir: B1_20260319_030000_7090kHz.wav
# (40m, 2026-03-19 03:00:00 UTC start, ~60 min total).
PARENT_RECORDING_START = {
    'B1': datetime(2026, 3, 19, 3, 0, 0),
}

# Map encoded band string in recording filename to RBN CSV band column.
BAND_MAP = {
    3590: '80m',
    7090: '40m',
    7091: '40m',
    10118: '30m',
    10191: '30m',
    14090: '20m',
    14091: '20m',
    18083: '17m',
    21090: '15m',
    24902: '12m',
    28090: '10m',
}


def parse_recording_filename(name):
    """Parse e.g. 'B1_seg2_15-30min_7090kHz.wav' → (parent_set, segment_index,
    start_offset_min, end_offset_min, band_khz, band_label).  Returns None on
    failure."""
    m = re.match(r'^(B\d)_seg(\d+)_(\d+)-(\d+)min_(\d+)kHz\.wav$', name)
    if not m:
        return None
    parent, seg, start_min, end_min, band_khz = m.groups()
    band_khz = int(band_khz)
    band = BAND_MAP.get(band_khz) or BAND_MAP.get(band_khz - 1)
    return {
        'parent_set':       parent,
        'segment_index':    int(seg),
        'start_offset_min': int(start_min),
        'end_offset_min':   int(end_min),
        'band_khz':         band_khz,
        'band':             band,
    }


def load_rbn_csv(csv_zip_path, csv_name):
    """Stream the RBN CSV; yield row dicts."""
    with zipfile.ZipFile(csv_zip_path, 'r') as z:
        with z.open(csv_name) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
            for row in reader:
                yield row


def derive_key(rec_info, csv_iter, consensus_min=2):
    """For a parsed recording, find the set of CQ-mode callsigns RBN
    observed on the matching band during the segment's time window,
    optionally filtered by a >= consensus_min distinct-spotter requirement.

    Returns (key_set, debug_stats_dict).
    """
    parent_start = PARENT_RECORDING_START.get(rec_info['parent_set'])
    if parent_start is None:
        raise ValueError(f"Unknown parent set {rec_info['parent_set']}; "
                         f"add to PARENT_RECORDING_START")
    win_start = parent_start + timedelta(minutes=rec_info['start_offset_min'])
    win_end   = parent_start + timedelta(minutes=rec_info['end_offset_min'])
    band      = rec_info['band']

    spotters = defaultdict(set)  # dx_call -> {spotter_call, ...}
    n_rows_in_window = 0
    for row in csv_iter:
        if row.get('band') != band:
            continue
        if row.get('mode') != 'CQ':
            continue
        ts = datetime.strptime(row['date'], '%Y-%m-%d %H:%M:%S')
        if ts < win_start or ts >= win_end:
            continue
        n_rows_in_window += 1
        dx = row['dx'].strip().upper()
        sp = row['callsign'].strip().upper()
        if not dx or not sp:
            continue
        spotters[dx].add(sp)

    if consensus_min > 1:
        key = {dx for dx, spots in spotters.items() if len(spots) >= consensus_min}
    else:
        key = set(spotters.keys())

    stats = {
        'window_start':    win_start,
        'window_end':      win_end,
        'band':            band,
        'rows_in_window':  n_rows_in_window,
        'unique_dx':       len(spotters),
        'unique_dx_consensus': len(key),
    }
    return key, stats


def write_key(key, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w') as f:
        f.write(','.join(sorted(key)) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('recordings', nargs='*',
                    help='Recording files.  Default: B1_seg*.wav under DEFAULT_RECDIR.')
    ap.add_argument('--csv-zip', default=DEFAULT_CSV_ZIP)
    ap.add_argument('--csv-name', default=DEFAULT_CSV_NAME)
    ap.add_argument('--keys-dir', default=str(KEYS_OUT_DIR), type=Path)
    ap.add_argument('--consensus-min', type=int, default=2,
                    help='Min distinct RBN spotters per call.  '
                    'Default 2 = VE7CC consensus rule.')
    ap.add_argument('--no-consensus', action='store_const', const=1,
                    dest='consensus_min',
                    help='Equivalent to --consensus-min 1 (include every dx call).')
    args = ap.parse_args()

    if not args.recordings:
        recdir = Path(DEFAULT_RECDIR)
        args.recordings = sorted(str(p) for p in recdir.glob('B1_seg*.wav'))

    print(f'Loading {args.csv_zip} (this may take a moment)...')
    # Buffer the CSV once since we need to re-iterate for each recording.
    rows = list(load_rbn_csv(args.csv_zip, args.csv_name))
    print(f'  loaded {len(rows)} rows')

    for rec in args.recordings:
        info = parse_recording_filename(Path(rec).name)
        if info is None:
            print(f'  SKIP {rec}: unrecognized filename pattern')
            continue
        key, stats = derive_key(info, iter(rows), args.consensus_min)
        out_name = (f'{info["parent_set"]}_seg{info["segment_index"]}_'
                    f'cq_key_rbn{args.consensus_min}.txt')
        out_path = args.keys_dir / out_name
        write_key(key, out_path)
        print(f'  {info["parent_set"]}_seg{info["segment_index"]}: '
              f'{stats["band"]} {stats["window_start"]:%H:%M}—{stats["window_end"]:%H:%M} '
              f'{stats["rows_in_window"]} RBN spots, '
              f'{stats["unique_dx"]} dx, '
              f'{stats["unique_dx_consensus"]} after consensus={args.consensus_min} '
              f'→ {out_path}')


if __name__ == '__main__':
    main()
