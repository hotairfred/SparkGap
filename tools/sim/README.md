# tools/sim — simulated CW bands with exact truth

Build a synthetic contest band, run SparkGap on it, and score the spots against
**exact ground truth**: which station ran, which only called, what each sent,
where. Real recordings can only be scored against what other RBN skimmers
happened to report; a scene knows everything.

1. `cellgen.py` — scripted CWT "cell" (one runner + its callers) as USB audio +
   truth (`CELL_FORMAT.md`, `sparkgap-cell/1`). Machine-perfect keying, per-station
   WPM/pitch/level, optional QSB. A stand-in until realistic cells (e.g. a Morse
   Runner CE robot runner) can be rendered in the same format.
2. `compose.py` — places cells across one receiver's span (analytic signal →
   resample to 192 kHz → shift → level), adds a band noise floor, writes 24-bit
   standard I/Q like the all-band recorder (#11) plus `<scene>.truth.json` with
   absolute RF for every station and message.
3. `score_truth.py` — classifies every spotted call: RUNNER (correct), CALLER (a
   station that only called — not a spot under RBN rules), WRONG_FREQ, BUST; and
   runner recall.

Example (12 cells, 40 m, 3 min):

```
cellgen.py cells/c0 --runner N8WCR --minutes 3 --seed 100 --calls pool0.txt
compose.py scene40.json scene40            # scene40_7030kHz_192000_24bit.wav + scene40.truth.json
sparkgap.py --config cfg.json --file scene40_7030kHz_192000_24bit.wav --center-khz 7030 \
            --start-min 0 --end-min 3 > run.log
score_truth.py run.log scene40.truth.json --list
```

Or replay it through the C path: `hpsdr_proxy.py --wav scene40_7030kHz_192000_24bit.wav --negate-q`.

Tune on scenes, confirm on real recordings: a simulator has its own quirks.

## Scenes: one copy of a call per receiver

Put each cell in a scene **once**. Several copies of the same cell in one receiver (for
example the same cell at three levels, 18 kHz apart) put the same calls on several
frequencies at once, and SparkGap's handling of one call on several frequencies then
decides which copies get spotted. That showed up as a false regression once. To test
levels, use one scene per level.
