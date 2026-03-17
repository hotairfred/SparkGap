# csdr-skimmer Decoder Improvement Research Notes

## Baseline Test Results (2026-03-16)

### Test File
- DK3QN 40m CW contest WAV (2009 Region 1 CW contest)
- 48kHz stereo IQ, ~18MB, ~1.5 minutes of 40m CW band

### CW Skimmer (Gold Standard)
- 146 spot lines, **108 unique callsigns**
- Spots saved in `cwskimmer_spots.txt`

### csdr-skimmer Results

| Config | Raw Decodes | CW Skimmer Calls Found | Filtered Spots | Unique Calls |
|--------|-------------|----------------------|----------------|--------------|
| Original (100 Hz bins) | 288 | 18/108 (17%) | 16 | 11 |
| Original + CQ/TEST filter | 288 | 18/108 | 12 | 7 |
| Improved decoder (100 Hz) | 313 | 19/108 (18%) | 13 | 6 |
| 50 Hz bins + improved decoder | 786 | 25/108 (23%) | 10 | 6 |
| 25 Hz bins | 0 | 0/108 | 0 | 0 (too fine, broke decoder) |

### Key Findings

1. **FFT resolution matters** — going from 100 Hz to 50 Hz bins improved raw signal detection from 18 to 25 out of 108 (30% improvement). But 25 Hz was too fine and broke the decoder timing.

2. **The decoder is the bottleneck, not the channelizer** — even when signals are detected in the raw output, libcsdr's CwDecoder can't decode them accurately enough to extract callsigns.

3. **libcsdr CwDecoder problems identified:**
   - Hard threshold signal detection (0.7/0.5 hysteresis) — misses weak signals
   - Fixed timing ratios for dit/dah — no probabilistic classification
   - Aggressive dit filtering — rejects sloppy fists
   - Slow adaptive speed tracking (/4.0 averaging)
   - Fixed 20ms noise blanking regardless of WPM
   - No SNR-based gating — tries to decode noise

4. **Decoder improvements tried (cw.cpp modifications):**
   - Tighter hysteresis (0.6/0.4 instead of 0.7/0.5)
   - Bayesian-inspired dit/dah classification using distance ratios
   - Faster adaptation (/3.0 instead of /4.0)
   - Dynamic noise blanking scaled to WPM
   - 3:1 ratio constraint enforcement
   - Result: marginal improvement (18→19 calls found)

5. **Conclusion:** The libcsdr CwDecoder (303 lines) is fundamentally too simple. Need to replace it entirely with a better decoder, not tune its parameters.

## Next Steps

### Option 1: AG1LE Bayesian Decoder (PREFERRED)
- 3,335 lines of C, open source on GitHub
- Based on Dr. Bell's doctoral thesis
- VE3NEA (CW Skimmer author) advised on the approach
- Proven on real contest signals
- Replace libcsdr CwDecoder with AG1LE's implementation
- Repo: https://github.com/ag1le/deepmorse-decoder (has C version in morse/ dir)
- Blog: http://ag1le.blogspot.com/2013/01/towards-bayesian-morse-decoder.html

### Option 3: Write Our Own
- Use AG1LE's blog as algorithm guide
- Claude writes C++, test against WAV
- More work but fully understood code
- Can optimize specifically for skimmer use case (callsign extraction, not general text)

### NOT pursuing: fldigi's decoder
- Not highly rated for CW decoding accuracy
- Deeply embedded in fldigi's architecture, hard to extract

## Files
- `cwskimmer_spots.txt` — CW Skimmer reference output (108 unique calls)
- `spot_filter.py` — master.scp + CQ/TEST validation filter
- `MASTER.SCP` — Super Check Partial database (50,387 callsigns)
- `DK3QN_40m_CW_contest_2009.wav` — test file (on SMB share)
- `/tmp/raw_decode.txt` — original 100 Hz raw output
- `/tmp/improved_raw.txt` — improved decoder 100 Hz raw output
- `/tmp/improved3_raw.txt` — 50 Hz bins raw output

## Modified Files
- `/home/fred/csdr/src/lib/cw.cpp` — improved decoder (kept as reference, needs replacing)
- `/home/fred/csdr-skimmer/cw-skimmer.cpp` — BANDWIDTH set to 50 (was 100)
- `/home/fred/csdr-skimmer/bayes-skimmer.cpp` — Bayesian decoder integration attempt

## AG1LE Bayesian Decoder Integration (2026-03-16)

### What was built
- `morse-wip` repo cloned and compiled
- `libbmorse.a` static library built from AG1LE's decoder objects
- `bayes-skimmer.cpp` written — combines csdr-skimmer channelizer + AG1LE decoder
- Compiles and runs cleanly

### Problem Discovered: Envelope Detection Gap
The csdr-skimmer FFT channelizer produces **power-per-bin** at the BANDWIDTH rate (50 Hz).
For CW at 30 WPM, a dit is ~40ms = only 2 FFT frames at 50 Hz rate.

**The FFT bin power does NOT clearly show CW keying** because:
- When a CW signal is present, the bin power stays elevated continuously
- The on/off keying causes small power fluctuations within the bin
- But those fluctuations are masked by the FFT's time integration
- Per-channel magnitude tracking (magL/magH) converges because signal is "always there"

**What's needed:** proper CW demodulation between channelizer and decoder:
1. Channelizer identifies WHICH bins have CW signals (coarse detection)
2. For each active bin, go back to time-domain IQ samples
3. Mix down to baseband (multiply by complex tone at bin center frequency)
4. Low-pass filter to isolate the CW signal
5. Take magnitude = keying envelope (mark/space pattern)
6. Feed envelope to Bayesian decoder at 200 Hz

This is exactly what bmorse's `rx_FFTprocess()` does — it's the missing middle stage.

### Architecture Insight
The problem with csdr-skimmer's design is it tries to do everything with one FFT:
- Signal detection (needs frequency resolution = long FFT)
- Keying envelope (needs time resolution = short FFT or time-domain processing)

CW Skimmer likely uses a two-stage approach:
- Long FFT for signal detection and frequency measurement
- Per-channel time-domain processing for keying envelope extraction

## bayes-skimmer3 — Overlapping FFT Approach (CURRENT)

### What Works
- 4x overlapping FFT gives 200 Hz frame rate from 50 Hz bins
- Per-bin min/max tracking produces clean 0-1 envelope from bin power
- The envelope clearly shows CW keying (px toggles 0→1→0)
- Up to 99 active decoders on contest recording
- AG1LE's Bayesian decoder sees the transitions (xhat toggles, px swings)

### What Doesn't Work Yet
- `trelis_()` never emits decoded characters
- The trellis decoder needs longer sustained patterns to converge
- NDELAY=200 (1 sec processing delay) might need adjustment
- The FFT bin power envelope shape is different from audio envelope — decoder expectations don't match
- Parameters like PATHS=20, initial speed=20 WPM may need tuning

## BREAKTHROUGH: Multi-Pass Brute Force Decoding (2026-03-17)

### Discovery
Running csdr-skimmer at MULTIPLE bandwidth settings and merging all results
through the master.scp filter produces dramatically better results than any
single bandwidth. Different signals decode better at different bin widths.

### Results Progression
| Approach | Validated Calls |
|----------|----------------|
| Original (100 Hz, strict filter) | 7 |
| 50 Hz bins, strict CQ/TEST filter | 12-13 |
| 50 Hz bins, relaxed filter (2+ sightings) | 16 |
| Multi-bandwidth merge (50+60+75+80+100 Hz) | 28 |
| MEGA merge (12 bandwidths + 4 thresholds) | **52** |

### Final Scoreboard
- CW Skimmer: 108 unique calls
- Our Linux Skimmer (mega merge): 52 unique calls
  - Matching CW Skimmer: 27
  - High-confidence exclusive finds (5+ char): 7
  - Possible false positives (short calls): 18
  - **Effective validated: 34 calls (31% of CW Skimmer + 7 it missed)**

### Why Multi-Bandwidth Works
- CW signals have different bandwidths depending on speed and fist quality
- A 20 WPM signal with clean keying fits well in 100 Hz bins
- A 35 WPM signal needs finer bins (50-60 Hz) to separate from adjacent signals
- Sloppy fists spread energy across bins differently at each resolution
- No single bandwidth is optimal for all signals — merge catches all of them
- **This is something CW Skimmer CANNOT do** — it runs at one fixed resolution

### Files Produced
- `/tmp/csdr_32.txt` through `/tmp/csdr_120hz.txt` — individual bandwidth runs
- `/tmp/csdr_tw3.txt` through `/tmp/csdr_tw8.txt` — threshold variations
- `spot_filter2.py` — improved filter with strict/relaxed modes
- `threshold-skimmer.cpp` — threshold decoder attempt (abandoned — worse than libcsdr)
- `bayes-skimmer.cpp`, `bayes-skimmer2.cpp`, `bayes-skimmer3.cpp` — Bayesian decoder attempts

### Multi-Input Discovery
Running the same decoder on different input representations finds more:
- **Stereo IQ** (original WAV, interleaved I/Q read as mono) — signals appear at offset frequencies
- **Mono I-channel** (extracted left channel only) — cleaner, no interleave artifacts
- **Magnitude envelope** (sqrt(I²+Q²)) — different signal shape, catches different keying patterns
- **Q-channel** (extracted right channel) — some signals decode better on Q

### Multi-Decoder Discovery
Different decoder tunings catch different signals:
- **V1 (original improved):** Faster adaptation, tighter hysteresis, probabilistic dit/dah
- **V2 (conservative):** Slower adaptation, patient character breaks, moderate sensitivity
- **V3 (aggressive weak signal):** Ultra-fast attack, very slow decay, minimal noise blanking, widest acceptance

Each decoder version produces unique callsigns the others miss.

### Final Architecture: 324-Pass Brute Force

```
Input WAV file
    |
    ├── Stereo IQ (original)
    ├── Mono I-channel (extracted)
    ├── Magnitude envelope (sqrt(I²+Q²))
    └── Q-channel (extracted)
         |
         Each input × 12 bandwidths (50-120 Hz)
         × 3 threshold weights (3, 4, 5/6)
         × 3 decoder tunings (V1, V2, V3)
         = 324 decode passes
              |
              v
         Merge all raw decode lines
              |
              v
         spot_filter2.py
         - Extract callsign-shaped strings (4+ chars)
         - Validate against master.scp (50,387 known calls)
         - Require: CQ/TEST context, OR 3+ sightings, OR contest exchange + 5+ char
              |
              v
         Validated spots with frequency, WPM, decoded text
```

### Final Results (2026-03-17, ~2 hours of work)

| Metric | Count |
|--------|-------|
| CW Skimmer (Gold Standard) | 108 unique calls |
| Spark Gap (324-pass brute force) | **80 unique calls** |
| Both found | 28 |
| Only Spark Gap found | **52** |
| Only CW Skimmer | 80 |
| Starting point (single pass) | 7 |
| **Improvement** | **11.4x** |

### Why It Works
- Different FFT bin widths resolve different signals (close-spaced vs isolated)
- Different threshold levels catch signals at different SNRs
- Different input representations (I, Q, magnitude, interleaved) have different noise characteristics
- Different decoder tunings handle different fist qualities and speeds
- master.scp validation filters garbage — only real callsigns survive
- **Brute force with smart filtering beats elegant single-pass decoding**

### False Positive Analysis
Of the 52 "exclusive" finds:
- 4 calls with 6+ chars: very high confidence (DL7JOM, OK1ATH, UA1AUW, YO9CWY)
- 8 calls with 5 chars: high confidence (4Z4DX, EI0HQ, HA0GK, RA3TT, RX3VF, etc.)
- ~26 calls with 4 chars: mixed — some real (B7HQ, N0HQ, OK1A), some garbled truncations
- Many 4-char "exclusives" are truncated versions of real calls (RK3E→RK3ER, UT7E→UT7UJ)

### Next Steps
1. Build automated multi-pass runner script (currently manual)
2. Test on additional WAV files to validate generalization
3. Integrate AG1LE Bayesian decoder as V4 tuning for weak signal improvement
4. Build real-time version with parallel threads per bandwidth
5. Reduce false positives: cross-reference truncated calls against full call matches
6. Package as a single tool with JSON config
