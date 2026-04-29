#!/usr/bin/env python3
"""Hourly 3-way goldkey scorer — rolls up OpenSkimmer vs SDC vs RBN.

Reads three local tee logs:
  /tmp/os_stream.log   — our own skimmer1 spot output (os_tee.py)
  /tmp/sdc_stream.log  — UT4LW SDC Skimmer on Fred's Flex (sdc_tee.py)
  /tmp/rbn_stream.log  — worldwide RBN telnet feed (rbn_tee.py)

For each hour boundary, computes the per-spot precision (per Fred's
criteria: hit if confirmed by SDC or RBN, miss if solo) and the
goldkey rate (when openskimmer + SDC agree, what fraction is also on
RBN). Appends to /tmp/score_loop.log.

Asymmetric coverage is fine — openskimmer scans more bands than SDC
on this rig, so plenty of legit catches will be solo-on-our-side.
We track the metric anyway; the trend matters more than the absolute.

Run: nohup python3 score_loop.py > /tmp/score_loop_run.log 2>&1 &
"""
import re
import time
from datetime import datetime, timezone, timedelta

OS_LOG  = '/tmp/os_stream.log'
SDC_LOG = '/tmp/sdc_stream.log'
RBN_LOG = '/tmp/rbn_stream.log'
OUT_LOG = '/tmp/score_loop.log'

# Lines look like:
#   HH:MM:SS DX de SPOTTER-#:    14025.50  CALL          CW   15 dB ...
# Pull the call (4th-ish whitespace token after the freq).
LINE_RE = re.compile(
    r'^(\d{2}:\d{2}:\d{2})\s+DX de \S+:\s+\d+\.\d+\s+([A-Z0-9/]{3,15})\s+(\S+)'
)


def calls_in_window(path, start_hms, end_hms, mode_filter='CW'):
    """Read a tee log, return the set of unique calls heard in
    [start_hms, end_hms) for the given mode."""
    out = set()
    try:
        with open(path) as f:
            for line in f:
                m = LINE_RE.match(line)
                if not m:
                    continue
                hms, call, mode = m.groups()
                if hms < start_hms or hms >= end_hms:
                    continue
                if mode_filter and mode.upper() != mode_filter:
                    continue
                out.add(call.upper())
    except FileNotFoundError:
        pass
    return out


def score_window(start_hms, end_hms):
    us  = calls_in_window(OS_LOG,  start_hms, end_hms)
    sdc = calls_in_window(SDC_LOG, start_hms, end_hms)
    rbn = calls_in_window(RBN_LOG, start_hms, end_hms)

    hits   = us & (sdc | rbn)
    gold   = us & sdc & rbn
    us_sdc = us & sdc
    solo   = us - sdc - rbn

    return {
        'us':      len(us),
        'sdc':     len(sdc),
        'rbn':     len(rbn),
        'hits':    len(hits),
        'gold':    len(gold),
        'us_sdc':  len(us_sdc),
        'solo':    len(solo),
        'precision': (len(hits) / len(us)) if us else 0.0,
        'goldkey':   (len(gold) / len(us_sdc)) if us_sdc else 0.0,
        'recall_vs_sdc': (len(us_sdc) / len(sdc)) if sdc else 0.0,
    }


def write_row(row, label):
    with open(OUT_LOG, 'a', buffering=1) as f:
        f.write(
            f"{label}  "
            f"us={row['us']:4d}  sdc={row['sdc']:4d}  rbn={row['rbn']:4d}  "
            f"hits={row['hits']:4d}  solo={row['solo']:4d}  "
            f"prec={row['precision']*100:5.1f}%  "
            f"goldkey={row['goldkey']*100:5.1f}%  "
            f"recall_sdc={row['recall_vs_sdc']*100:5.1f}%  "
            f"(us∩sdc={row['us_sdc']}, gold={row['gold']})\n"
        )


def hour_window(end_dt):
    """Return (start_hms, end_hms) for the hour ending at end_dt."""
    start_dt = end_dt - timedelta(hours=1)
    return start_dt.strftime('%H:%M:%S'), end_dt.strftime('%H:%M:%S')


def next_hour_boundary(now):
    """First UTC top-of-hour strictly after now."""
    return (now.replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=1))


def main():
    while True:
        now = datetime.now(timezone.utc)
        target = next_hour_boundary(now)
        sleep_s = max(1.0, (target - now).total_seconds())
        time.sleep(sleep_s)
        # Score the hour that just ended.
        end_dt = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start_hms, end_hms = hour_window(end_dt)
        row = score_window(start_hms, end_hms)
        label = end_dt.strftime('%Y-%m-%d %H:00 UTC')
        write_row(row, label)


if __name__ == '__main__':
    main()
