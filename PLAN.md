# OpenSkimmer plan

_Current as of 2026-04-29._

## What we have

5-band CW + FT8 + RTTY skimmer running on a Red Pitaya STEMlab 125-14
under Linux. Bayesian CW decoder (named ITILA after MacKay's textbook,
original to this project). Standard DX-cluster spots on telnet :7300 in
SkimSrv-compatible wire format ... banner spoofed to satisfy Aggregator's
version + grid parsers. `rbn_feeder.py` posts spots directly to
`x.reversebeacon.net:88` via JSON HTTP, replacing VE3NEA's Aggregator
on Linux.

## Current quality

| Metric | Value | Notes |
|---|---|---|
| Goldkey (us+SDC → RBN agrees) | **95-98%** | When two skimmers concur, the world agrees |
| Recall vs SDC | 22-72% | Highly band / time dependent |
| Per-spot precision | 16-39% | Solo bucket has the long noise tail |
| FT8 spots / cycle (5 bands) | 75-77 | Steady, zero UDP loss |

Goldkey is the trustworthy number. Solo precision is the open work item.

## Active priorities

1. **Multi-day data collection** via the 3-way scoring loop
   (`os_tee.py`, `sdc_tee.py`, `rbn_tee.py`, `score_loop.py`). Hourly
   rollup to `/tmp/score_loop.log`.
2. **Build empirical blacklist** from calls that consistently appear
   solo (not on SDC, not on RBN) across many hours. Append to
   `blacklist.txt`.
3. **Re-introduce `gate_patt3ch_filter`** once a clean baseline is
   documented. Structural-pattern allowlist via SkimSrv's `patt3ch.lst`
   should drop the country-prefix-damaged tail without touching real
   calls.
4. **Email N4ZR for supervised RBN test.** Draft is parked outside the
   repo. Hold until gates are tightened and goldkey trend is documented.
5. **Then go live** on production RBN with `rbn_feeder.py`.

## Hardware reality

Pitaya 125-14 has roughly 45 dB less dynamic range than the Flex 6700
SDC runs on. Some weak-signal recall gap is physics, not code.
70-80% of SDC's recall on contested bands is the realistic ceiling
... the last 10% is intermod, not an algorithm bug.

## Anti-patterns to refuse

- Don't pivot architecture on a single-recording metric without the
  measurement protocol below.
- Don't blame the antenna, band, or conditions before exhausting the
  code (Fred's law).
- Don't merge stale SCP data ... silent keys cause false positives.
- Test on real audio (proxy WAV replay or live capture). Synthetic
  signals lie.
- Keep file-mode and live-mode config in sync. Silent divergence has
  burned us before.
- Don't add a feature flag without an off-default and a measured win
  against the off-default.

## Measurement protocol

For any "X improves Y" claim:

1. Both arms run over the same wall-clock window with all three tees
   capturing.
2. Fresh capture for each comparison ... do not reuse old logs.
3. Score per-spot precision and goldkey separately. Goldkey trend
   matters more than absolute solo precision.
4. Categorize misses: (a) no bin near the freq, (b) bin present but
   noise, (c) bin + raw text contains the call signature but the
   extractor failed, (d) bin + raw text substituted to a wrong call.
   Each tells a different fix.
5. One change at a time.

## Quality bar (industry context)

- VE7CC's 2+ skimmer rule is the goldkey methodology, validated.
- MASTER.SCP is soft validation, not a strict filter (per the RBN team).
- `patt3ch.lst` is the structural-pattern tier we plan to add.
- Industry baseline precision is 70-80% per RBN-OPS lessons. Goldkey
  says we clear it on cross-validated catches; the solo bucket is what
  we're working on.
