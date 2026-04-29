# OpenSkimmer architectural plan

Last revised: 2026-04-26 (after the PFB session retrospective)

## The pattern this document exists to break

Going back through `MEMORY.md` and the session arcs, the project keeps
repeating the same loop:

1. Diagnose the real problem (decoder accuracy at low SNR / weak DX).
2. Get distracted by an attractive piece of infrastructure work
   (multi-decoder ensemble, per-bin scanner, now PFB) that *feels* like
   it should help.
3. Build the infrastructure cleanly. Measure. Realize the underlying
   decoder problem is still there.
4. Apply layers of patch-fixes (fuzzy SCP, lower SNR, multi-sighting
   tuning) that mask symptoms but don't address the root cause.
5. Hit a different wall (CPU, ring drops, false-positive flood). Pivot
   to a new architecture. Go to step 1.

Specific instances:
- bmorse + uhsdr ensemble was abandoned for ITILA.
- ITILA's per-bin scanner replaced PFB.
- This session: PFB is being added back, on top of ITILA's per-bin
  channelizer.
- Each pivot promised to fix weak-DX decode. None have. The hypotheses
  in `feedback_cw_weak_signal_decoder_gap.md` (Apr 25, priority-ordered)
  remain UNTESTED.

## What we actually want from the skimmer

In priority order:

1. **High-precision RBN feed.** Each emitted spot should be a real
   station the operator could work. False positives waste RBN
   downstream and erode trust.
2. **Comparable recall to SDC on the bands we cover.** SDC is the local
   benchmark and is on the same RF (split antenna). Hardware gap
   (Flex 6700 vs Pitaya, ~120 dB vs ~75 dB dynamic range) explains
   *some* miss but not 7 of 14.
3. **Stable under load.** Multi-band, multi-mode (CW + FT8 + RTTY), no
   ring drops, no segfaults, no decoder thrashing during contest peaks.
4. **CPU headroom for growth.** Room to add more bands or more decoders
   without re-architecting.

Goals 1 and 2 pull in opposite directions. Tighter validation reduces
recall; relaxed validation increases false positives. The priority
order above means precision wins ties.

## The diagnosed core problem (from Apr 25)

Quoting `feedback_cw_weak_signal_decoder_gap.md`:

> Of 26 SDC-only calls checked across our entire log, **24 had ZERO
> decodes ever**. The bins ARE firing at those frequencies. We just
> produce garbage text instead of clean callsigns.

Sample pattern: `'A I E EI H E EP E ?8,V?DG E HD EHS??5I'` at the
frequency where SDC cleanly decoded HB9SO. Single-letter dits/dahs
without enough SNR to resolve into characters.

**This is not a channelization problem. This is a decoder problem.**
PFB doesn't fix it. Per-bin scanner doesn't fix it. Ensemble doesn't
fix it. Each architecture re-runs into the same wall.

## Hypotheses (Apr 25 priority order, all still UNTESTED)

1. **Channel filter too wide → adjacent strong signal saturation.**
   - Per-bin scanner Stage 2 LPF: 75 Hz (post FIR75 commit `1ce6feb`)
   - PFB at n_chan=2048: 94 Hz (this session)
   - PFB at n_chan=4096: 47 Hz (later this session)
   - **CORRECTION (2026-04-26 17:30 per Grayline's binary analysis):**
     SkimSrv does NOT use narrow channels — they use 500 Hz PFB bins
     PLUS a per-channel Goertzel at 600 Hz audio pitch for in-bin
     frequency selection.  Earlier memory entries citing "SDC ~25-30 Hz"
     were a wrong guess that propagated.  See
     `feedback_envelope_decoder_arch.md` for the full corrected picture.
   - For envelope-based decoding (us), narrower channels DO help avoid
     summing co-channel signals — but the proper architectural fix is
     adding fine-frequency selection per active bin.

2. **AGC saturation by adjacent strong signals.** If AGC window is
   shared or wide, a strong signal sets the gain that suppresses weak
   signal energy elsewhere.

3. **`ev_thresh` too strict for weak signals.** Currently 1.5. May
   over-suppress legit decodes; try 1.0 or 0.8.

4. **Bayesian decoder algorithm gap.** ITILA's Markov approach may
   underperform matched-filter or HMM at low SNR. SkimSrv uses
   Bayesian too but with different priors / state model.

5. **Sample-rate / decimation chain quality.** Alias/imaging may hurt
   low-SNR detection.

## The PFB-on-top-of-ITILA architectural concern

Fred's question, restated: ITILA already has a channelizer (per-bin
NCO + 3-stage FIR). Adding PFB upstream means **two channelizers in
series**. Does that hurt weak signals?

Yes, in two specific ways:

1. **Channel BW gets WIDER, not narrower.** PFB at n_chan=2048 has
   94 Hz channel BW. Even with ITILA's 75 Hz LPF downstream, the
   *combined* selectivity is bounded by PFB's 94 Hz — you can't
   recover narrower selectivity by filtering further down. The dual
   100/200 Hz path that ITILA was designed around degenerates to a
   single ~94 Hz path. We lose the bandwidth-diversity benefit.

2. **Frequency grid gets COARSER.** ITILA's per-bin scanner places
   bins at 50 Hz multiples *anywhere*, sub-bin interpolated from the
   energy scan. PFB has fixed 94 Hz channels; signals that fall
   between bin centers (more than ±47 Hz from any center) are
   invisible. Even with a fine-tune NCO mix per active bin, the *raw
   PFB output* has already band-limited to ±47 Hz around the chosen
   bin center. A signal 60 Hz off the nearest bin lands in a stopband.

The CPU savings from PFB (one FFT batch vs N per-bin FIR chains) and
the bin-density headroom (PSC_MAX_BINS=1024 vs ITILA_MAX=200) are real,
but they only matter if we're CPU- or bin-bound. Right now we're
**decoder-accuracy-bound**, and PFB makes that worse.

## What we did this session, scored against this plan

| Change | Status | Verdict |
|---|---|---|
| `pfb_scanner.{c,h}` + libpfb_scanner.so | committed `19636a5` | architecture-defensible but doesn't address current bottleneck |
| `hpsdr_fast.c` per-RX scanner_feed/decode | committed `19636a5` | **real bug fix; keep** |
| `_is_base_call` length cap 4-7 | committed `253b289` | **clean win; keep** |
| Leading-letter SCP correction | committed `253b289` | masks decoder issues; explicitly listed as a known dead-end in `feedback_cw_weak_signal_decoder_gap.md` |
| Prefix-extension SCP correction | committed `1701f70` | same: masks decoder issues |
| MAX_WPM=50 enforcement | committed `1701f70` | **defined-but-unused gate now active; keep** |
| signal_min_snr 12 → 8 | reverted (config) | known dead-end, confirmed: 30% ring drops |

Yesterday's hypothesis priority list (the actual diagnosed problem):
**ZERO of 1-5 touched.**

## Systematic plan — each phase is a checkpoint, NOT to be skipped

### Phase 0 (added 2026-04-26 03:30 ET): is the decoder actually the problem?

Fred raised the right question: ITILA decodes recorded IQ cleanly but
fails live.  If true, the decoder isn't broken — something between
antenna and decoder is.

**Experiment** (do this BEFORE Phase 1):

1. Live mode running on a band with known-failing weak-DX signal
   (G8X at 7020.5 with 3 dB SDC SNR is a recurring example).
2. Bare-C IQ capture (`c_capture.c` → 60 sec float32 IQ) of that
   exact band/time window.
3. Replay through the SAME scanner code path: `openskimmer.py
   --file <capture> --center-khz <band> --start-min 0 --end-min 1`.
4. **Decision rule:**
   - Replay produces clean text on the same signals live garbled →
     bug is in the live delivery path (sample drops we don't see,
     ring/timing artifact, IQ scaling, concurrent-load coupling).
     Phase 1 (filter) is the wrong investigation.
   - Replay also produces garbage → decoder/channelization itself
     is the problem.  Proceed to Phase 1.

This was the methodology in the Apr 25 session that found the
per-RX worker bug.  Apply it again before assuming anything about
the decoder.

Specific things to look at if "live ≠ recorded":
- HPSDR UDP packet loss before our ring (kernel /proc/net/udp drops)
- Per-RX worker stalls (sample timing under load — env_drops continued
  growing in tonight's session at low SNR even when ring_drops looked
  stable; the env_drops counter is a sample-loss signal we underused)
- Concurrent-load coupling: 5 bands + FT8 minute thread + RTTY scan
  all in the same process; recorded replay is single-band quiet
- Bin spawn FIR transient: first ~16 samples after spawn are
  FIR-stage-1 delay-line warmup, not real signal — if a signal's
  decode window starts within those samples, decode quality drops

### Phase 1: Validate hypothesis #1 (channel filter)

**One change, measured, decided.**

1. Tighten `FIR_S2_100` design from 75 Hz cutoff to 40-50 Hz. Rebuild
   `libitila_scanner.so`. Deploy on **per-bin scanner bands** only
   (PFB band stays as-is for control).
2. Restart skimmer. Wait for steady state (10 min minimum).
3. Pull fresh 15-min SDC capture (192.168.1.205:7373) during EU peak
   (08-12 UTC weekdays, or any contest period).
4. Diff: per-band overlap count, weak-signal raw-decode quality at
   SDC's catch frequencies.
5. **Decision rule:**
   - Overlap on per-bin bands UP → narrower selectivity is right; chase
     it further (try 30 Hz, then evaluate PFB at higher n_chan with
     equivalent BW).
   - Overlap UNCHANGED → not the bottleneck; advance to hypothesis #2.
   - Overlap DOWN → revert; advance to hypothesis #2.

### Phase 2: Hypothesis #2 (AGC saturation)

Only after Phase 1 is concluded. Same one-change-measure-decide
discipline.

### Phase 3: Hypothesis #3 (`ev_thresh`)

Tune up or down based on Phase 1/2 findings.

### Phase 4: Hypotheses #4-5 only if #1-3 don't close the gap

Algorithm-level work (priors, decimation, alternative decoder) is
expensive. Justify with measured residual gap, not speculation.

### Phase 5: ONLY after decoder gap is bounded — revisit channelization

If hypothesis #1 confirms narrower BW is the answer, then PFB at
**higher n_chan** (4096 → 47 Hz BW, or 8192 → 23 Hz BW) becomes the
right architecture. Until then, PFB on 40m stays as a single-band A/B
control.

## Anti-patterns to refuse, with reasons

| Anti-pattern | Why it bites |
|---|---|
| Pivot to new architecture before measuring current one | Repeats the loop this document exists to break |
| Lower `signal_min_snr` below 12 globally | Per-bin scanner per-RX worker can't keep up; ring drops to 30% |
| Add fuzzy SCP correction layers | Masks decoder issues, makes false positives look like real spots, makes the next debug harder |
| Restart skimmer mid-measurement | Invalidates the comparison window |
| Claim improvement from a single 5-min window | Activity varies hugely; need 15+ min, ideally during peak |
| Skip a hypothesis because it "feels" wrong | Apr 25 deferred hypothesis #1 to "next session" — we are now in next session's next session and #1 is still untested |

## Measurement protocol (binding for any architecture claim)

1. **Window length**: 15 minutes minimum.
2. **Activity context**: peak EU (08-12 UTC) or contest period
   preferred. If forced to measure off-peak, label the result as
   such; do NOT compare cross-context.
3. **Source of truth**: SDC at 192.168.1.205:7373. Pull a fresh
   capture for each comparison; do not reuse.
4. **Categorization for each SDC spot we miss**: (a) no bin near that
   freq, (b) bin present but raw is pure noise, (c) bin + raw text
   contains call signature but extractor failed, (d) bin + raw text
   substituted to a wrong call. Each tells us a different fix.
5. **One change at a time.** Co-changes obscure causality.
6. **Capture skimmer log line offset BEFORE the comparison window**,
   so the diff is over the same wall-clock interval as the SDC
   capture.

## Hardware reality checkpoint

SDC runs on a Flex 6700 split off the same antenna. Flex front-end is
roughly 45 dB better dynamic range than the Pitaya. Some recall gap on
weak signals near strong signals is **physics, not code** — Pitaya's
front end intermod-distorts before SDC's does. This caps our
upside; do not chase the last 10% of SDC's recall as a bug.

A reasonable target is 70-80% of SDC's recall on contested bands,
with our false-positive rate equal-or-lower.

## What this session actually accomplished (after rescoping)

- Real bug fix in `hpsdr_fast.c` per-RX hookup.
- Length-cap and `MAX_WPM` gates committed.
- PFB infrastructure built but its appropriateness for the *real*
  bottleneck is now in doubt (Phase 1 will tell us).
- Memory updated with the loop pattern and the priority-order
  hypothesis list.
- This document.
