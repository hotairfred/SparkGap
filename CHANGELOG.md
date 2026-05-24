# SparkGap changelog

Pre-1.0 alpha. No versioned releases yet — entries are dated.

## 2026-05-24

### Reverted
- **`sk_5band.json`: `signal_min_snr` 8 → 12** (rolled back ~20 min after
  deploy of cc2daf7).  The file-mode A/B showed +3 CQer recall with
  contained FPs and clean SCP-valid mix, but production behavior was
  catastrophically different: after 18 min on skimmer1 with all 8 bands
  live, `ring_drops=92.7%`, `env_drops=389M`, every scanner pinned at
  `peak=400`, CPU 575% on 6 cores, load avg 9.37.  The C-side IQ ring
  buffer was overflowing because the decoder couldn't keep up — fewer
  spots reached the output than the deployed cq_runner-bypass baseline
  (~1/min vs 2.91/min), the opposite of what the file-mode test
  predicted.  Root cause: file-mode runs one band single-threaded; live
  runs 8 bands concurrently and the SNR=8 threshold spawns enough
  additional bins to push every scanner to max_bins=400 at the same
  time, blowing the per-RX-worker decode budget (the failure mode
  documented in feedback_bin_saturation_ceiling.md).  At SNR=12 we were
  within budget at 400 bins; at SNR=8 we're not.

### Lessons recorded
- **File-mode B1_seg2 A/B does not characterize live multi-band
  bin-pressure.** B1_seg2 is a single 40m recording at 7090 ±100 kHz.
  Live load is 8 bands × 400 max_bins.  Any change that increases bin
  spawn rate needs a *production-density* test before deploy — either
  via a longer multi-band capture replay, or a short live-shadow with
  rollback hot.  Tested-but-not-shipped: cluster_hz 150→50, same risk
  profile; should not deploy without an actual ring-drop measurement at
  production density.
- **Watch `ring_drops` and `env_drops` post-deploy.**  The decisive
  signal was the Health: log line, not spot count.  CW spot rate looked
  *low* (1/min vs 2.91 baseline) — easy to mistake for "CW activity is
  quiet now" until I checked the Health line.  Going forward, the
  post-deploy 15-min check needs a Health-line summary, not just a spot
  count.

## 2026-05-23

### Fixed
- **CW spot suppression in SpotTracker** (sparkgap.py, commit `4cbc048`).
  `_is_freq_leader` was designed to suppress decoder hallucinations of
  one real call (K4R / K4RU / K4RUM from a single K4RUM CQ) by emitting
  only the call with most recent sightings at a freq.  Side-effect: when
  callers spotted first ratcheted the leader count up, the actual runner
  never overtook.  Worked example on B1_seg2 7040.9 kHz: K0TG (real CQer)
  decoded in 40 windows, extracted as cq_runner 9 times, never spotted —
  KK4E / W6SX / VE3KP cycled through as leaders, K0TG got only 1 sighting
  per appearance and lost every leadership check.
  Fix: the call identified as cq_runner via `_itila_extract_cq_call`
  bypasses `_is_freq_leader`.  Hallucinations of the runner still
  compete normally (they have different SCP buckets).  File-mode A/B
  on B1_seg2 0-15min: 44/56 → 49/56 recall (+8.9 pp), 174 → 229 spots,
  zero spots lost.  Production effect: deployed to skimmer1 at 21:15 UTC,
  CW spot rate jumped from 0.90/min to 2.91/min (3.2× lift) measured
  in the first 19 min.  Recovered five known CQers (K0TG, K1RV, N5XZ,
  NQ2W, W4SPR) on B1_seg2.

- **`_ItilaScanner.collect()` per-entry pending flush** (sparkgap.py,
  same commit).  Defensive correctness: previously joined all pending
  entries into one tracker.process() call with `''.join()`, which
  conflated multiple window events.  When two CQers' windows accumulated
  between collect_all calls, `_itila_extract_cq_call` on the joined text
  tie-broke by "most-frequent, last" and picked the wrong runner —
  silently suppressing the earlier one via the cq_runner gate.  Now
  yields one result per pending entry; each tracker invocation sees
  exactly one window's data.  Doesn't fire on B1_seg2 (windows don't
  accumulate between collects in that workload), but real and worth
  closing.

### Added
- **README "How the decoder works" section** (commit `8df7c89`).
  Names the actual algorithm — *2-state HMM forward-backward + EM
  parameter estimation + 16-bin WPM marginalization + late thresholding
  + 64-wide beam search* — rather than just referencing "ITILA" (which
  is MacKay's textbook).  Spells out the pipeline end-to-end with file
  pointers (`itila_core.c`, `fb_core.c`, `itila_scanner.c`).  Explicitly
  notes what's deliberately NOT done (word-level prior fusion during
  beam search — tested 2026-05-23 via `research/fusion_per_window.py`,
  +0.1 pp lift, open question).  Addresses external review feedback that
  readers couldn't tell the decoder did soft keystate without going to
  the .c source.

- **`research/fusion_per_window.py`** — diagnostic measuring per-window
  fusion lift directly from beam-dump data.  Replaces the original
  per-freq-aggregated diagnostic at `research/beam_fusion_diag.py` which
  compared "first window's text0" against "any beam at any window" (an
  asymmetric comparison that overstated the rescuable population by
  ~10).  Correct per-window number on B1_seg2: +0.10 pp lift, 4 rescues
  per 4080 windows.  Per-CQer ceiling identical between baseline and
  fusion (52/56 = 92.86% — both find the same calls in text0 of some
  window across 15 min).

- **`docs/scanner_tracker_ipc_refactor.md`** — design doc for replacing
  the synthetic-text channel between `_ItilaScanner` and `SpotTracker`
  with structured `SpotIntent` records.  Captures the bug class
  (string-IPC ambiguity → tracker re-parsing → conflated per-event
  gates) and the migration plan.  Refactor partially implemented on
  branch `sparkgap-ipc-refactor` (commit `e67cd10` on atlas).

### Changed
- **Repo history cleaned of NAS credential `Cl4ude01`** via
  `git filter-repo`, rewriting 248 commits' versions of `run_cwt.sh`
  (Grayline driving).  All branches re-SHA'd accordingly.  Backup at
  `/mnt/atlas/skimmer/openskimmer.git.bak-20260523-225822-prefilter`.

## 2026-05-15

### Added
- **Morse-aware character-confusion gate on SCP correction**
  (`sparkgap.py` _MORSE_TABLE / _MORSE_CONFUSABLE). Letter-level edit-1
  was incorrectly treating aurally-distinct letter pairs as neighbors —
  e.g. N=`-.` and W=`.--` are 3 ins/del operations apart in dit-dah
  space but letter-edit-1 considered them edit-1, silently rewriting
  raw decodes like NT2J to WT2J because WT2J was the only edit-1
  neighbor in MASTER.SCP. The new gate constrains every character swap
  in SCP correction (both `_scp_bucket` general path and
  `_correct_leading_letter` first-character path) to pairs within
  `_MORSE_MAX_INDEL=2` insert/delete operations on the dit-dah sequence.
  Captures real CW failure modes (fade eats an element, noise spike
  inserts one) — E↔I, R↔N↔G, S↔H, B↔D, U↔V, C↔K all ≤2 — while
  rejecting aurally distinct pairs (N↔W=3, I↔W=3, H↔W=5, K↔V=3).
  Validated against MASTER.SCP (50,419 entries): NT2J cluster returns
  None instead of WT2J; legit single-char noise corrections
  (1F8Z→WF8Z, C3LR→K3LR, B5RZ→N5RZ) still resolve.
- **Short-target SCP bucket-substitute guard exclude_self mode.**
  `_has_recent_band_support(... exclude_self=True)` for the short-
  target guard prevents cross-skimmer collective hallucination: when
  `OS:self` was counted as a corroborator, our own past emission of a
  noise-substituted 3-char call would self-vouch any future
  substitution, and any RBN peer worldwide that also hallucinated the
  same call satisfied the 2-spotter minimum. Now the short-target
  guard requires `_rb_min_spotters` *external* peer skimmers. S-floor
  sighting-threshold path keeps the default `exclude_self=False`
  (own confirmation still anchors S-floor).

### Fixed
- **G5E noise flood through cross-skimmer corroboration.** 7-hour
  observation post-length-aware-fix showed 24 G5E emitted despite 69
  SUPPRESSED (peer cache including `OS:self` lowered the bar).
  Confirmed zero G5E emit after exclude_self fix shipped 00:40 UTC.

## 2026-05-14

### Added
- **ITILA timing-cost confidence gate** (ggmorse-inspired). Per-decode
  signal that flags windows where ITILA's segmentation looks ratty even
  if the resulting string matches an SCP entry. Designed to gate the
  M5M / G7D / N3T failure mode where decoder noise threshold-crosses and
  "decodes" to a short callsign by chance.
  - **Algorithm:** for each decode window, normalise run lengths by class
    average (dit / dah), sum squared deviations from canonical 1-dit /
    3-dah / 1-unit-gap Morse timing, plus a proportional penalty if
    `avg_dah / avg_dot` drifts outside the canonical [2.5, 3.5] window.
  - **Validated on B1_seg2 (40 m CWT recording):** real in-key calls
    peak at cost 15 (a 60-WPM operator); the 3-char structural noise
    class (M5M-shape garbage) clusters at median cost 19, p75 36.
    `cost > 30` is a zero-FN gate that drops ~11 % of garbage including
    most of the M5M-class.
  - **C vs Python parity:** 96.9 % gate agreement on 130 real channels,
    median absolute delta 0.043, zero in-key disagreements.
  - **Config:** new `gate_timing_cost` (bool, default `false` in code,
    flipped to `true` in `sk_5band.json` after first 7 h of observation)
    and `timing_cost_max` (float, default 30.0; production threshold
    set to 100.0 to match the observed live distribution — the
    M5M-class hallucinations cluster above 85, while real emissions
    stayed under 75). Cost is *always logged* on each ITILA decode
    (`ITILA raw kHz cost=X.XX …`).
  - **API:** new `double itila_get_last_cost(itila_t)` in `itila.h`.
  - **C-worker plumbing.** `libitila_scanner.so` exposes
    `itila_sc_set_cost_fn()`; `ScDecodeResult` extended 280→288 bytes
    with trailing `double cost`; `libhpsdr_fast.so` `RESULT_SIZE`
    bumped accordingly; Python `_ItilaScanner.run` parses cost from
    offset 280 and applies the gate before emission.
- **Length-aware SCP bucket-substitute guard** (M5M class). When the
  substitute target is ≤3 chars, require at least `_rb_min_spotters`
  peer-skimmer corroborations on the same band within
  `_rb_window_sec` before allowing the substitution. Short SCP
  entries are noise magnets (each position has ~62 edit-1 neighbours
  so random decoder output routinely lands next to one). Combined
  with the cost gate, kills the M5M / G5E / G7D laundering pattern
  while leaving long-target corrections (VM4FO → KM4FO etc.) intact.
  New `gate_short_scp_bucket` flag (default `true` in code, no JSON
  override needed).
- **Validation tooling:** `eval_timing_cost.py` (scan a WAV, dump
  `(call, freq, cost, in_key)` CSV + threshold sweep) and
  `compare_c_vs_py_cost.py` (parity harness between C library and
  Python prototype).
- **README Development section** — explicit note that SparkGap is
  developed using Claude Code as the primary development tool
  (matches NereusSDR / AetherSDR / GTBridge convention).

## 2026-05-13

### Fixed
- **Memory leak in `SpotTracker._rb_support` (S-floor peer cache)** — root
  cause of the 2026-05-13 OOM kill. The RBN worldwide telnet tee fed every
  spot worldwide into `_ingest_support`, which wrote
  `_rb_support[(bucket, band)][spotter] = ts` with no eviction.
  `_has_recent_band_support` filtered stale entries at read time but never
  deleted them. Six days × hundreds of spots/minute = ~1.7 GB/day growth,
  hit kernel OOM at 14.5 GB RSS after 6-day uptime. Fix: new
  `SpotTracker._sweep_rb_support(now)` method that prunes spotters older
  than `_rb_window_sec` and drops empty `(bucket, band)` keys; called every
  5 min from main loop. Validation: RSS plateaus at ~3.4 GB within 5 min
  of restart (was monotonically rising before).
- **8-hour silent dead-air problem** — the OOM kill went unnoticed for
  8 hours because there is no liveness alarm. Memory note saved for a
  watchdog (systemd `--user` Restart=on-failure + MQTT heartbeat that
  comms_watch can alarm on) but not yet shipped.

### Added
- **RSS heartbeat in Status block** (`sparkgap.py:6506`). Reads VmRSS from
  `/proc/self/status` every 30 s, logs `Mem: rss_kb=NNN rb_buckets=NNN`.
  Provides curve data for future leak diagnosis + regression detection
  on the S-floor cache size specifically.
- **Operator-reported blacklist entries** — 5 calls added under a new
  2026-05-13 "Operator-reported" block: `N3T, A5N, SA6P, E6AQ, M5M`.
  All confirmed in `/tmp/sparkgap.log` as decoder noise on short
  MASTER.SCP entries. M5M was the smoking gun: 4 different noise inputs
  (X5M / K5M / N5M / I5M) all bucket-substituted to the same target in
  5 hours. Memory note saved to revisit `gate_scp_bucket_substitute`
  behaviour for sub-4-char SCP targets (likely a length floor of ≥4).

## 2026-05-07

### Changed
- **`ppm_offset` reset to 0.0** for clean baseline measurement. W3RGA Day 4
  flagged us at +5.3 ppm; we applied `ppm_offset = 5.3` and Day 5 came back
  at −4.8 ppm — a sign-flip overshoot. Interpretation: the +5.3 was thermal
  transient from the 04-30 8-band switch-on (more receivers active = more
  TCXO heat = more drift). After 24 h soak the natural drift had settled
  to roughly +0.5 ppm, so the −5.3 correction overshot. Resetting to 0.0
  lets W3RGA Day 6 reveal the actual stable thermal-equilibrium drift
  before we pick a permanent offset.
  - **Day 6 confirmed: +0.1 ppm** (rank #41/199, 3545 spots, 0.34% dupe).
    Same skew as W3RGA himself. Best calibration figure we've ever shown.
    Both hypotheses validated — sign was right, magnitude was thermal.
    Leaving `ppm_offset = 0.0` as the steady-state value.

### Fixed
- **Stale hardcoded ppm constant in startup banner** (`sparkgap.py:6007`).
  The "SparkGap LIVE: 3590000 (3589.986 kHz)" line was multiplying centre
  frequencies by `0.9999961` (a baked-in 3.9 ppm correction), independent
  of the actual `ppm_offset` config — leftover from a pre-`ppm_offset`
  era. Cosmetic only; the spot emit path already routes through
  `_corrected_freq_khz()` and was honouring the JSON config correctly.
  Banner now also routes through `_corrected_freq_khz()`.

## 2026-04-30

### Added
- **Blacklist v2 (70 calls)** mined from a 30+ hour 3-way score-log
  window. Methodology-driven (no hand edits): a CW call goes on the
  list iff our decoder emitted it ≥ 3 times across ≥ 2 hours and
  neither SDC nor worldwide RBN ever heard it. 1×1 pattern (`@#@`)
  filter for the high-confidence cut. v1 was 44 entries; v2 added
  27 (P3Z×12, G8N×9, G4U×8, M7Q×6, M9N, G9D, M4E, etc.) and removed
  1 (N8A — graduated out, peer-corroborated in the wider window).

### Fixed
- **Blacklist bypass via SCP bucket-substitute.** When
  `gate_scp_bucket_substitute` is on, decoder noise like `K7A` gets
  mapped to its edit-1 nearest SCP entry `C7A`. The original
  blacklist check fired on the raw `K7A`, but the emit path saw the
  substituted `C7A` — so blacklisted calls leaked through whenever
  they were the substitution target. Added a re-check after
  substitution. Verified with debug logging: `BL-SUPPRESS` fired
  6× (M2T, G5Q, S0S, V6D ×2) within an hour of deployment.
- **Blacklist loader** was including comment lines as "calls"
  because `line.strip().upper()` doesn't strip leading `#`.
  `Database: ... + 52 blacklisted` (vs the 44 actual entries) was
  cosmetic but confusing during the bypass investigation. Now
  strips end-of-line comments before checking for empty.

## 2026-04-29

### Added
- **Native RBN feeder (`rbn_feeder.py`)** — Aggregator-equivalent on
  Linux. Wire format derived from a local capture:
  plain HTTP on `x.reversebeacon.net:88`, JSON POSTs to `/rx/6/id.php`
  (registration heartbeat every 50s) and `/rx/6/s.php` (spot batch
  every ~10s). About 340 lines, three threads (local read / heartbeat
  / upload). No Wine, no .NET, no closed-source binaries in the path.
- **SkimSrv impersonation in `telnet_server.py`** — pre-login banner,
  `SKIMMER/SETT` response, `CwSkimmer >` prompt, `CU AGN!` disconnect.
  Aggregator (and any future SkimSrv-aware downstream) accepts our
  cluster output as a valid Primary Skimmer source.
- **3-way scoring tooling** — `sg_tee.py`, `score_loop.py`. Hourly
  rollup of sparkgap vs SDC vs RBN catches: per-spot precision,
  goldkey rate (when we + SDC agree, RBN agrees too), recall vs SDC.
- **`score_diff.py` A/B comparison tool** — takes two HH:MM[:SS]
  windows, prints metric deltas (precision, goldkey, recall) plus
  optional per-call movers (promoted / demoted / new solo).
- **Recent-on-band support floor (S-floor)** — adapted from N2WQ's
  GoCluster cross-source validation work. New `gate_recent_band_floor`
  (off by default), per-(call_bucket, band) cache populated by
  background daemon threads tailing peer DX-cluster telnets (SDC
  on .205:7373, worldwide RBN on telnet.reversebeacon.net:7000).
  When gate is on AND a call has ≥ min_spotters distinct peer
  spotters within window_sec on the same band, threshold drops
  to 1 sighting. Cache warms regardless of gate flag.
- **Harmonic suppression** — adapted from N2WQ's GoCluster. New `gate_harmonic_filter` (off by default).
  Per-call recent-fundamental history; on each emit checks whether
  the spot is a 2x-5x integer multiple of a recent same-call spot
  at appropriately weaker SNR. Phantom-mode telemetry counts
  would-suppress events. (As of 2026-04-30, count is 0 across
  multiple hours including CWT density — Pitaya RX chain isn't
  generating same-call harmonic spurs at our SNR threshold.)
- **Blacklist v1 (44 calls)** mined from 18 hours of 3-way
  score-log data. 1×1 noise calls our decoder emitted repeatedly
  with no SDC or worldwide-RBN corroboration.
- **README expansion** — macOS and Windows quick-start sections,
  "Feeding the RBN" section pointing at `rbn_feeder.py` with the
  RBN-OPS-coordinate-first caveat.
- **Author attribution** in README.

### Fixed
- **SDC peer / sdc_tee recv timeout** bumped 120s → 600s. SDC has
  consistent ~126s silent gaps that were just-overshooting the
  120s timeout, churning both `_peer_connect_loop` and `sdc_tee.py`
  every 2-5 minutes. Cosmetic for correctness (state rebuilt cleanly
  on reconnect), but cleaner logs and slightly tighter S-floor
  cache coverage.
- **`itila_scanner.c` bin eviction** — lazy spawn (3-hit FFT gate)
  was added previously, but eviction only fired at `n_bins >=
  max_bins`. Under typical operation each band stays well below cap,
  so eviction never ran and bins drifted up over hours until FFT cost
  starved the decoder (ring drops climbed to 61% at 96-minute uptime).
  Now sweeps every scan and evicts any bin idle for >300s. Verified
  steady-state at ~60 bins / 0.037% ring drops over 8+ hours uptime.
- **Telnet wire format** — single space between `dB` and the WPM
  number (was double). Aggregator's parser was rejecting our SNR/WPM,
  emitting placeholder 0 dB / 18 WPM upstream.
- **SCP bucket trim** no longer chains edit-1 — preserves country
  prefixes (IT9DV stays IT9DV instead of becoming R9DV).
- **Bucket-consistency between sighting record and lookup** — count
  functions now bucket the lookup key the same way the storage does.
- **README miscredit** — was crediting "ITILA decoder authors" as
  if there were an existing project. ITILA is MacKay's textbook; the
  decoder is original to this project.

### Removed
- `RESEARCH_NOTES.md` (700+ lines of March/April baseline research
  superseded by the codebase; recoverable from git history).
- `comms.md` from the public repo (private inter-Claude file, kept
  locally and gitignored).
- Hardcoded NAS password in `run_cwt.sh`.

### Changed
- `PLAN.md` rewritten as a current-state snapshot. Old version was
  narrating decoder-architecture pivots that have since shipped or
  been abandoned.
- `TODO.md` rewritten — collapsed historical "Done" sections, kept
  active items, trimmed backlog to genuinely future work.
- 6-character grid (`EM79SM`) in default config — Aggregator and
  RBN expect the full Maidenhead.
- **README acknowledgments scrub** — Aggregator is by W3OA (Dick
  Williams), not VE3NEA. CWSL_Tee is by HrochL (Czech Republic),
  not VE3NEA. Dropped VE3NEA from the acknowledgments entirely
  since SparkGap doesn't actually use any of his code; CW
  Skimmer is conceptual inspiration only, not a dependency.
- **README framing rewrite** — drop hearsay, fix SDC-Connectors
  facts, trim Community section to a clean thank-you.

## 2026-04-25 → 2026-04-26

### Added
- **5-band live operation** sustained on a Pitaya 125-14
  (CW + FT8 + RTTY).
- **FT8 live integration** — replaced ring-buffer corruption with
  per-RX double-buffer + atomic swap. 75-77 spots / cycle across
  5 bands, zero UDP loss.
- **RTTY MVP** — FT8-style minute snapshot, letter-first regex,
  multi-cycle confirmation, fuzzy variant collapsing.
- **Lazy bin spawn** — require 3 consecutive FFT-scan hits before
  allocating a bin. Drops env_drops 24% → 0.05%, CPU 378% → 154%,
  enables max_bins ceiling raise to 400.
- **Per-RX CW worker threads** — single worker was serializing 5 RX
  streams through ITILA decode. Per-RX workers cut sample loss from
  50% to 2.3%, CW spot rate 4 → 12.2 / minute on 20m.
- **Caller-spotting from QSO context** — extracts both runner and
  caller. Took recall 74% → 91% vs CW Skimmer on file replay.
- **Configurable gate flags** in `sk_5band.json` — every
  precision/recall trade-off is an operator-tunable knob with
  ship-it defaults (permissive, trust cluster filtering).

### Fixed
- **C receiver** — `libhpsdr_fast.so` per-RX hookup; 9941 packets/s,
  8 DDCs × 192 kHz.
- **Thread races in `itila_core.c`** — fixed.
- **WPM cap** for spurious 60 WPM frenzy from envelope detector
  ringing on dead-antenna conditions.

## Earlier

See `git log` for the project's early history (March 2026 onward).
Major themes: decoder-quality iteration (CFAR, ITILA, Bayesian
framework, FIR chain), receiver pipeline, evaluation methodology,
and getting from "single-band proof of concept" to "5-band live
production".
