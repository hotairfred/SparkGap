#!/usr/bin/env python3
"""Replay a live run's transcripts (live_voice.py --transcripts DIR, DIR/segs.jsonl) through the live rules
without the GPU: each over goes to Runner.rule() in the order it was transcribed. Environment switches
(VS_*) pass through, so a rule change can be measured in seconds instead of an hour of transcription.
usage: live_rule_replay.py DIR/segs.jsonl REC_START_UTC [--fcc FILE] > spots.tsv   (REC_START_UTC 2026-10-25T02:00:01)
Output lines: recording time, kHz, call, copies (the live spots file format, lag 0)."""
import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ap = argparse.ArgumentParser()
ap.add_argument('segs')
ap.add_argument('start')
ap.add_argument('--fcc', default=None)
a = ap.parse_args()
import live_voice as lv   # noqa: E402

r0 = dt.datetime.fromisoformat(a.start).replace(tzinfo=dt.timezone.utc).timestamp()
args = argparse.Namespace(centre=0.0, lo=0.0, hi=0.0, port=0, call='TEST', spots=None, fcc=a.fcc, transcripts=None,
                          no_lockon=False, batch=0)
r = lv.Runner(args)
out = []
r.emit = lambda g: out.append(g)
for line in open(a.segs):
    khz, t0, segs = json.loads(line)
    n = len(out)
    r.rule(khz, t0, segs)
    for g in out[n:]:
        t = dt.datetime.fromtimestamp(r0 + t0, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        side = 1 if g['khz'] >= 10000 else -1
        print('%s\t%.1f\t%s\t%d\t0' % (t, g['khz'] + side * lv.CARRIER_OFFSET_HZ / 1000, g['call'], g['n']))
