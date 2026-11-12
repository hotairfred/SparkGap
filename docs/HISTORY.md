# SparkGap history

Moved from the README on 2026-11-12. It describes the decoder chain SparkGap ran from March to
September 2026 and the comparisons made with it; the numbers are not current.

SparkGap grew out of luarvique's csdr-skimmer. From March to September 2026 it ran the
original ITILA decoder with a configurable set of spot gates; itila2 and the repeat spot rule
became the default chain in October 2026 (`cw_decoder: itila2`, #62); `cw_decoder: itila`
still selects the original one. The material below describes
that earlier chain and the comparisons made with it. It is kept for reference; the numbers
are not current.

## March 2026 comparison (B1_seg2, original decoder)

> **Reference comparisons** (single 15-min recording: B1_seg2, 40m,
> 2026-03-19 03:15–03:30 UTC, scored two ways for the local-vs-global
> distinction operators care about):
>
> - **`recall_local` — vs the same-antenna decoder ceiling**: against
>   the SkimSrv-curated 56-call CQ key (what CW Skimmer's offline
>   mode caught on the same audio, hand-filtered to runner CQs):
>   **92.9%** (52 of 56, slash-tolerant). This is the fair
>   decoder-quality metric — it controls for "what our antenna could
>   even theoretically hear at this site that night."
>
> - **`recall_global` — vs RBN worldwide consensus**: against the
>   RBN historical CSV (≥2 distinct skimmers confirming, 153 unique
>   calls after slash-normalization): **49.7%** (76 of 153). Lower
>   bounded by propagation/antenna — RBN's distributed nodes hear
>   stations our single 40m loop physically could not.
>
> - **Vs CW Skimmer's own RBN recall**: CW Skimmer offline mode on
>   the same audio caught **22.9%** of the RBN-153 set (35 calls).
>   On `recall_global`, SparkGap is roughly **2× CW Skimmer** on
>   this recording. **47 RBN-validated calls** SparkGap caught that
>   CW Skimmer missed; **6** CW Skimmer caught that SparkGap missed.
>
> - **Precision side**: 274 unique SparkGap spots, 96 (35%)
>   independently validated by RBN or CW Skimmer; 178 are
>   unverified — mix of real local stations (RBN missed) and false
>   positives.  Live mode disambiguates these via SDC tee; the file
>   replay does not.
>
> - **The "local but not global" bucket exists**: of those 96
>   validated spots, **20 are calls where SparkGap and CW Skimmer
>   independently agreed but RBN missed entirely** — two different
>   decoders, different algorithms, both saw the signal that no
>   worldwide skimmer ≥2-consensus-confirmed. That's empirical
>   evidence for the "RBN-only scoring underweights local coverage"
>   argument: an antenna-co-located decoder catches stations RBN's
>   distributed network doesn't.
>
> - **Live deployment** alongside UT4LW's SDC-Connectors (actively-
>   maintained closed-source alternative) on a shared antenna split:
>   matches 67% of SDC-Connectors' spots in the first 17 minutes of
>   operation.  Each catches some calls the other doesn't — they hear
>   different pieces of the same band.
>
> SDC-Connectors has years of contest tuning we don't yet match. We
> have features it doesn't (open source, multi-mode in one process,
> packet-loss telemetry). One recording is one recording — these
> numbers are calibration, not a contest result. Methodology in
> `tools/eval/b1_benchmark.py` and `eval_rbn_validated.py`.
>

## The original ITILA decoder

The CW decoder ("ITILA" in the source — named after MacKay's *Information
Theory, Inference, and Learning Algorithms* textbook that motivated the
approach, not a single named algorithm) is a **Bayesian, soft-decision
keystate decoder**.  It maintains continuous `P(key=down | t)` posteriors
all the way through to character matching rather than thresholding the
envelope into bits at any intermediate stage.

The pipeline, per channel:

1. **Channelization.** Per-bin IIR LPF on the FFT-decimated baseband,
   producing a real-valued envelope at 200 Hz.
2. **EM parameter estimation.** A two-state Gaussian-mixture EM iterates
   the noise mean (`nm`), signal amplitude (`A`), observation variance
   (`σ²`), and dit unit length (WPM) for this channel.  Warm-starts from
   the previous window's converged parameters when available.
3. **HMM forward-backward.** Given the EM-fit observation model and a
   two-state Markov transition matrix derived from the estimated WPM,
   `fb_core` (C extension) computes the per-sample posteriors
   `γ[t,0] = P(space | observation)` and `γ[t,1] = P(mark | observation)`.
   These are continuous, never thresholded inside the decoder.
4. **Speed marginalization.**  Sixteen WPM bins span 8-60 WPM; the
   forward-backward log-evidence is computed at each bin and used to
   marginalize γ across speed hypotheses (`gamma_marg`).  Multi-station
   density triggers GMM WPM detection (M8): two candidate WPMs decoded
   in parallel so a slow runner doesn't lose to a faster caller's clock.
5. **Late quantization.** `γ_marg[:,1] > 0.5` produces the binary
   mark/space stream — the *only* threshold in the whole decoder, and it
   happens once the posterior has integrated evidence across the full
   window.
6. **Beam-search run-length decode.** A 64-wide beam search over the
   mark/space run-lengths consumes Morse-element timing log-likelihoods
   (canonical dit / dah / inter-element / inter-character / inter-word
   gaps) plus an ambiguity-aware emission model.  Returns the top-K
   decoded text hypotheses.
7. **Callsign extraction.** Three-pass regex+SCP scan of the decoded
   text: literal SCP match, length-bounded substring scan, and a
   Morse-distance-aware edit-1 correction (e.g. `T1LZ → K1LZ`).
8. **Adaptive consensus gates.**  Outside the decoder, in `SpotTracker`,
   per-frequency leader / CQ-runner / harmonic / blacklist / patt3ch
   gates filter what becomes a spot.  Every gate is a config flag so the
   operator can tune precision/recall.

What this means relative to other open-source CW decoders:

- **Not threshold-based.** Threshold decoders (cwdaemon, fldigi's
  default CW) make a hard mark/space decision per sample and lose
  posterior evidence.  Skimmer's reputation rests on *not* doing that;
  SparkGap doesn't either.
- **Speed is marginalized, not assumed.** Many decoders need the
  operator to set WPM (or auto-detect on training data once).  SparkGap
  re-fits per window, every window, with a 60 s context.
- **Soft keystate, but post-decode SCP fusion is shallow.**  Right now
  SCP / patt3ch corrections are applied *after* the beam decode picks a
  winner.  Combining word-level priors *during* beam selection (BALUN
  vs BALAN style) is a known open area — tested on a 15-min CWT
  recording (`research/fusion_per_window.py`); +0.1 pp recall in that
  dataset, but the test conditions weren't dense enough to exercise it.
  Open question.

Source: `itila_core.c` (full implementation, ~1400 lines C),
`fb_core.c` (forward-backward kernel), `itila_scanner.c` (band-wide
channelizer + lazy bin-spawn), and `_ItilaScanner` + `SpotTracker` in
`sparkgap.py` for the Python orchestration.

## Gate flags (`cw_decoder: itila`, `spot_rule: off`)

The decoder behavior is fully configurable.  Defaults are "ship-it"
mode (permissive emit, trust cluster filtering).  Each gate can be
re-enabled individually for stricter local behavior:

| Flag | Default | What it does |
|---|---|---|
| `enable_caller_spotting` | `true`  | Extract callers AND runner from QSO context (high-recall mode) |
| `gate_cq_runner` | `false` | Forces runner-only when CQ is in buffer (lower recall, higher precision) |
| `gate_freq_consensus` | `false` | Per-freq adaptive consensus + 5-min commit lock |
| `gate_patt3ch_filter` | `false` | Reject bypass calls not matching SkimSrv's `patt3ch.lst` patterns |
| `gate_bypass_consensus` | `false` | Bypass calls go through freq consensus vs simple count threshold |
| `gate_scp_bucket_substitute` | `true`  | Emit nearest-SCP bucket form instead of raw decoded call |
| `gate_recent_band_floor` | `false` | S-floor: anchor solo decode if peer skimmers spotted the same call on the same band recently |
| `gate_harmonic_filter` | `false` | Drop apparent 2x-5x harmonic spurs of recent same-call spots (RX intermod artifacts) |
| `gate_telemetry` | `true`  | Log "would-gate" decisions even when gate is off (diagnostic) |
| `gate_timing_cost` | `false` (in code; `sk_5band.json` overrides to `true`) | Drop ITILA decode windows whose segmentation timing cost exceeds `timing_cost_max`. B1_seg2 validation suggested 30; production shipped at 100 (median emit-cost in production was higher than the recording's, so 100 catches the obvious M5M-class flood without clipping marginal real calls). Targets the M5M / G7D class where decoder noise threshold-crosses and "decodes" to a short callsign by chance. Cost is logged on every decode regardless of the flag. |
| `gate_short_scp_bucket` | `true` | When a SCP bucket-substitute target is ≤3 chars, require peer-skimmer corroboration before allowing the substitution. Short SCP entries are noise magnets (G5E, M3E, M5A class); without this guard, decoder garbage routinely lands edit-1 from one of them. Skipped for ≥4-char targets. |

Plus the standard knobs:

- `signal_min_snr` — bin spawn threshold (default 12 dB; lower = more weak DX, more CPU)
- `itila_max_bins` — concurrent bin ceiling per band (`sk_5band.json` ships at 400; 200 is conservative)
- `scp_bypass_threshold` — sightings required for non-SCP-validated calls
- `enable_ft8` — runs FT8 + RTTY pipelines (RTTY currently piggy-backs)
- `ft8_band_cut` — `fft` (default): each band's 12 kHz FT8 audio is cut from one FFT of the minute, single precision; `resample`: the original mix of the whole minute plus `resample_poly`
- `ft8_windows` — `aligned` (default): two 15 s windows around each FT8 slot, placed from the snapshot's start time; `sliding`: the original, every second from 0 to 45 s, 46 decodes a band-minute. Aligned windows take the slots from this PC's clock and cover a transmission from about 1 s early to 2.3 s late, so keep the clock synced (NTP or chrony); a clock a second or more off loses FT8 decodes. Use `sliding` for replays unless `hpsdr_proxy.py --align` lines the recording up with the clock

