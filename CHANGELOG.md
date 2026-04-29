# OpenSkimmer changelog

Pre-1.0 alpha. No versioned releases yet — entries are dated.

## 2026-04-29

### Added
- **Native RBN feeder (`rbn_feeder.py`)** — replaces VE3NEA's Aggregator
  on Linux. Reverse-engineered the protocol from a Wireshark capture:
  plain HTTP on `x.reversebeacon.net:88`, JSON POSTs to `/rx/6/id.php`
  (registration heartbeat every 50s) and `/rx/6/s.php` (spot batch
  every ~10s). About 340 lines, three threads (local read / heartbeat
  / upload). No Wine, no .NET, no closed-source binaries in the path.
- **SkimSrv impersonation in `telnet_server.py`** — pre-login banner,
  `SKIMMER/SETT` response, `CwSkimmer >` prompt, `CU AGN!` disconnect.
  Aggregator (and any future SkimSrv-aware downstream) accepts our
  cluster output as a valid Primary Skimmer source.
- **3-way scoring tooling** — `os_tee.py`, `score_loop.py`. Hourly
  rollup of openskimmer vs SDC vs RBN catches: per-spot precision,
  goldkey rate (when we + SDC agree, RBN agrees too), recall vs SDC.
- **Author attribution** in README.

### Fixed
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
