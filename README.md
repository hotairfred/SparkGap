# SparkGap

*(Formerly OpenSkimmer. Not affiliated with SparkSDR by M0NNB.)*

Open-source, Linux-native, multi-band skimmer for amateur radio: **CW, RTTY and FT8** on
several bands at once, with DX-cluster spots on telnet and an uploader for the Reverse Beacon
Network (RBN).

> **Status: beta.** In daily use on a Red Pitaya STEMlab 125-14 at WF8Z skimming 8 bands
> (80, 40, 30, 20, 17, 15, 12 and 10 m), feeding a local cluster and PSKReporter (FT8).
> **Not connected to RBN.** The RBN team has given test criteria SparkGap must pass before a
> node may connect, including identifying itself as SparkGap by name and version; that work
> is tracked in #49. Until then, keep `rbn_feeder.py` off.

## Where it stands

Measured on our own recordings, replayed through the production receiver path and scored
against what other RBN skimmers reported at the same time:

- **Precision.** About 95% of the calls SparkGap spots are also spotted by another RBN skimmer
  within 1 kHz, across 7 recordings (CWTs, an MST, the PA QSO Party, quiet overnights; the
  tables are in #61 and #67). CW
  Skimmer nodes nearby run about 95 to 98% on the same measure. The rest are a mix of real stations
  nobody else heard and busts, mostly calls cut short (K1V for K1VUT), which current decoder
  work targets.
- **Recall.** Against a CW Skimmer node 20 miles away, on the same evenings, SparkGap spots
  1.6 to 1.9 times as many calls and catches about 70% of that node's calls (#43).
- **Copy on known text.** Character error on three W1AW code-practice clips: 3.1, 3.7 and
  1.9% (`docs/ITILA2.md`; CW Skimmer copies the same audio nearly perfectly).
- **Cost.** 8 bands at 192 kHz with FT8 and RTTY take about 2.2 cores of a 35 W i5-9500T with
  `itila_decode_threads: 4` (profile in #59).

These are calibration on our station, not a contest result. The harness to reproduce them is
in `tools/eval/` and `tools/sim/`.

## What it does

- **Receivers.** HPSDR protocol 1 radios (a Red Pitaya with Pavel Demin's `sdr_receiver_hpsdr`
  firmware, up to 8 receivers at 48 to 192 kHz each), and FlexRadio DAX-IQ through `flex_iq.py`
  (`skimmer_flex.example.json`).
- **CW.** A scanner finds every signal in each band and opens one channel on it; each channel
  is decoded by **itila2**, a two-state HMM decoder with its timing fitted from the sender's own
  keying (`docs/ITILA2.md`).
- **Spotting the runner, not the caller.** The repeat spot rule (`spot_rule.py`) follows what
  RBN asks of a skimmer node: only the station calling CQ or TEST is spotted, after 2, 3 or 4
  copies by RBN's call pattern file (`patt3ch.lst`), with lock-on (who owns the frequency) and
  CQ qualifiers (`CQ PA`, `CQ FD`). Callers are never spotted.
- **RTTY.** RTTY signals are found in each band's RTTY segment and decoded once a minute, with
  the same repeat rule.
- **FT8.** Decoded on each band's FT8 frequency with `decode_ft8` from Karlis Goba's
  [ft8_lib](https://github.com/kgoba/ft8_lib) (MIT) and uploaded to PSKReporter
  (`pskr_feeder.py`).
- **Outputs.** DX-cluster spots on telnet port 7300 in CW Skimmer Server's format (RTTY lines as
  RTTY Skimmer Server writes them), so loggers, clusters and Aggregator can read them; an
  optional second port for your own use with its own, looser spot rule (see the quick start);
  `rbn_feeder.py` uploads CQ-tagged spots to RBN directly (keep it off until the RBN criteria
  above are passed).
- **Options for the spot rule**, all off by default: a validation score using calls RBN
  skimmers have heard and the SKCC roster (`spot_validation`, `docs/ITILA2.md`), split DX
  signing `TU <call> UP` (`spot_rule_split_up`), back-to-back CQs counted each
  (`spot_rule_cq_repeats`), `CWT <call>` without CQ (`spot_rule_contest_words`), and pieces of
  longer calls held back (`spot_rule_fragments`).
- **SparkGap Web**, a read-only browser console with waterfall, call ladder and live decode
  text (`gui_feed`, `docs/CONSOLE.md`).
- **Tools.** An all-band IQ recorder (`iq_recorder.py`), a proxy that replays recordings as an
  HPSDR radio (`hpsdr_proxy.py`), file-mode replay, a band simulator with exact ground truth
  (`tools/sim/`), and scoring against the RBN archive (`tools/eval/rbn_confirm.py`).

Work in progress is in the [open pull requests](https://github.com/hotairfred/SparkGap/pulls).

## Quick start (Linux, Red Pitaya 125-14)

Dependencies (Debian/Ubuntu):

```
apt install build-essential gcc python3 python3-numpy python3-scipy \
            libfftw3-dev pkg-config
```

PSKReporter and MQTT need `paho-mqtt>=2.0`, which Ubuntu doesn't package; use
`pip install -r requirements.txt` in a venv.

Build the C libraries (`make native ARCH=native` for `-march=native`):

```
make native
```

FT8 needs `decode_ft8` from [ft8_lib](https://github.com/kgoba/ft8_lib): `make ft8` fetches it
(pinned to the commit tested) and builds it beside `sparkgap.py`. To use one elsewhere, set
`"ft8_decoder": "/path/to/decode_ft8"`; or set `"enable_ft8": false`. SparkGap logs a warning
at start if FT8 is on and the decoder is missing.

Pitaya: install [Pavel Demin's `sdr_receiver_hpsdr`](https://pavel-demin.github.io/red-pitaya-notes/sdr-receiver-hpsdr/)
firmware and make sure it's reachable on your LAN.

Configure: copy `sk_5band.json` and edit at least:

```json
{
  "callsign": "YOUR_CALL",
  "grid": "YOUR_GRID",
  "sdr_ip": "192.168.X.X",
  "bands": [3590000, 7090000, 10118000, 14090000,
            18083000, 21090000, 24902000, 28090000],
  "cw_decoder": "itila2",
  "itila_decode_threads": 4,
  "enable_ft8": true,
  "enable_rtty": true,
  "rtty_spot_rule": "repeat"
}
```

`recent_band_floor.peers` ships empty. Any spot source you add there (another cluster on your
LAN, RBN's telnet) is connected at start even with `gate_recent_band_floor` off.

`cw_decoder: itila2` (the default) is the current decoder with the repeat spot rule;
`cw_decoder: itila` selects the original chain (see *History*).

Run:

```
python3 sparkgap.py --config sk_5band.json
```

Connect to the spot stream:

```
telnet localhost 7300
```

Port 7300 (`telnet_port`) carries the RBN-grade spots, the same stream `rbn_feeder.py`
uploads. If you want your own spots filtered differently, for example a looser rule for your
cluster or logger, add a second port with its own rule:

```json
"telnet_local_port": 7301,
"local_spot_rule": {"doubled_calls": true, "split_up": true, "fewer_repeats": 0}
```

`split_up` also spots DX working split from its sign-offs (`TU CY0S UP`), which CW Skimmer
does not do. Both port numbers are yours to pick (`--port` and `--local-port` override them).
Tweaks to `local_spot_rule` change only the local port; what goes to RBN stays the same.
Without `local_spot_rule`, 7301 carries the same spots as 7300. FT8 and RTTY go to both ports.
The local rule can also set its own `validation`, `validation_calls`, `cq_repeats` and
`contest_words`; without them it follows the top-level keys.

**FlexRadio:** copy `skimmer_flex.example.json`; it uses a DAX-IQ channel, so CW Skimmer can
keep another channel on the same radio. If another receiver hangs on one of the radio's receive
loops, set `flex_loopa`/`flex_loopb` (and `flex_rxant`) to match: SparkGap's pan and slice come up
with the radio's band setting, and a loop switched off there is off for the whole antenna.

**macOS** should work with light Makefile changes (`.dylib` instead of `.so`; on Apple Silicon
omit `-march=native`). **Windows:** WSL2 with Ubuntu, then the Linux steps. Native builds for
both are welcome contributions.

## Feeding a cluster or RBN

Port 7300 speaks the same telnet format as CW Skimmer Server, so a DX cluster (we use
GoCluster), a logger or Aggregator can connect to it.

RBN: SparkGap is not yet authorized to connect. The checks a node must make itself before a
spot reaches RBN (CQ only, repeats, mode codes, beacons, frequency) and what we measured are in
#49. `docs/rbn-feeding.md` describes `rbn_feeder.py` for when that work is done.

## Replaying and measuring

File mode decodes a recording as fast as the CPU allows:

```
python3 sparkgap.py --config sk_5band.json --file recording.wav --center-khz 7090 \
                    --start-min 0 --end-min 15
```

It reads 16- and 24-bit stereo IQ WAV files at 96 or 192 kHz. Multi-band runs go through the
production C receiver path when the recording is replayed with `hpsdr_proxy.py` from another
host (`--negate-q` for skimmer1 captures). Public test recordings are in the repository's
releases (CY0S working split on 40m, a hand-sent 30m PA QSO Party segment, W1AW clips with
true text on the contributor fork).

Other tools: `rbn_tee.py` logs the worldwide RBN telnet feed; `SIGUSR1` to a running instance
dumps each band's IQ buffer to `/tmp`.

## Development

Contributions welcome: open an issue, then a small pull request with measurements. Tests run
with `pytest` (`tests/`) and in CI on Python 3.10 and 3.12. SparkGap is developed with Claude
Code as the main tool.

## License

GPLv3. See `LICENSE`.

## Authors

- Fred Krause, **WF8Z** (EM79SM), project owner. Contact info is on QRZ.
- Chris White, **WX7V**: the itila2 decoder and scanner, the repeat spot rule, the FlexRadio
  path and SparkGap Web.

## History

SparkGap grew out of luarvique's csdr-skimmer. From March to September 2026 it ran the
original ITILA decoder with a configurable set of spot gates; itila2 and the repeat spot rule
became the default chain in October 2026 (`cw_decoder: itila2`, #62); `cw_decoder: itila`
still selects the original one. The material below describes
that earlier chain and the comparisons made with it. It is kept for reference; the numbers
are not current.

### March 2026 comparison (B1_seg2, original decoder)

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

### The original ITILA decoder

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

### Gate flags (`cw_decoder: itila`, `spot_rule: off`)

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

## Acknowledgments

This project stands on a lot of prior work — both software and theory.
Where source files are derived from another project, the original
copyright headers and license remain in those files; the list below is
the high-level acknowledgment.

The current decoder is **itila2** (Chris White, WX7V), built on the original **ITILA**
decoder (original to this project, named after MacKay's textbook). Several other decoders
ship in this tree as alternative paths and as research baselines —
they are not currently in the live spotting pipeline, but they are
included in the source distribution and their authors deserve credit
regardless.

### Software the live pipeline depends on

- **Pavel Demin** — Red Pitaya HPSDR firmware (`sdr_receiver_hpsdr`)
  and the `ft8d` standalone FT8 decoder.
- **Rudy Bakalov (N2WQ)** — *GoCluster*, BSD-licensed cluster server.
  Cross-source validation techniques (recent-on-band support floor,
  harmonic suppression) in this project's spot pipeline are adapted
  from GoCluster's design.

### Decoders shipped as alternative paths / research

These are present in the tree but not in the active pipeline. Each
file retains its original headers and license.

- **AG1LE (Mauri Niininen)** — *bmorse*, a Bayesian Morse decoder
  (`bmorse-skimmer.cpp`, `bmorse_lib.cxx`, `bmorse_src/`,
  `libbmorse.so`). Original copyright 2012–2014, GPL-3.0. Used during
  decoder research.
- **TF3LJ (Loftur E. Jonasson)** and the **UHSDR Firmware Project** —
  the in-process UHSDR CW decoder (`uhsdr_cw_lib.cpp`, `uhsdr_cw.cpp`,
  `libuhsdr_cw.so`). Original copyright 2016, GPL-3.0.
- **W1HKJ (Dave Freese)**, **AG1LE (Mauri Niininen)**, **OH2BNS
  (Tomi Manninen)**, **VE7IT (Lawrence Glaister)** — the *fldigi* CW
  decoder. `fldigi_cw.cpp` is a standalone port of fldigi's `cw.cxx`,
  GPL-3.0.
- **Marat Fayzullin (luarvique)** — *csdr-skimmer*, the project
  SparkGap was forked from (GPL-3.0). Its original CW and RTTY skimmer
  programs are still here: `cw-skimmer.cpp`, `rtty-skimmer.cpp` and
  `bufmodule.cpp`, built by `make csdr` and packaged as
  `csdr-cwskimmer` and `csdr-rttyskimmer`.
- **HA7ILM (Andras Retzler)** and **Marat Fayzullin (luarvique)** —
  *libcsdr* / *csdr-cwskimmer*. `libcsdr_cw.cpp` derives from this
  work, BSD-licensed.
- **Erik de Castro Lopo** — *sndfile-spectrogram*, code referenced by
  bmorse.

### Theory and framework

- **David MacKay** — *Information Theory, Inference, and Learning
  Algorithms* (Cambridge University Press, 2003). The Bayesian
  framework behind the ITILA decoder comes from there; the decoder
  itself is original to this project.

### Community

- Everyone who's run a CW skimmer for the last 15 years and kept the
  RBN ecosystem alive.
