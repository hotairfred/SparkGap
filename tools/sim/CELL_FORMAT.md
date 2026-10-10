# SparkGap simulated-band "cell" format, v1 (draft 2026-10-08)

A **cell** is one runner + its pileup, rendered as receiver audio, plus a truth file.
The band composer (Spark Gap) places many cells at different RF frequencies and
levels, sums them with a noise floor into 192 kHz IQ, and maps truth to absolute RF.

## Files
`<name>.wav` + `<name>.json`, same basename.
Optional per-station stems: `<name>.<CALL>.wav` (same length/rate), lets the
composer apply per-station QSB/levels and render clean single-signal clips.

## Audio (`<name>.wav`)
- Mono, real audio, as a USB receiver would output it: a tone at audio pitch **p Hz**
  is a signal at **RF = cell dial + p**. (Composer converts to analytic baseband.)
- Any sample rate ≥ 8000 Hz (MRCE's 11025 is fine), 16-bit PCM or 32-bit float.
- No AGC/limiting baked in if avoidable; the composer sets levels.
- Background noise optional; the composer adds the band noise floor. If the cell
  includes noise, say so (`"noise_included": true`).

## Truth (`<name>.json`)
```json
{
  "format": "sparkgap-cell/1",
  "generator": "MRCE <version/commit>",
  "seed": 12345,
  "sample_rate": 11025,
  "duration_s": 600.0,
  "contest": "CWT",
  "noise_included": false,
  "stations": [
    {"call": "K1XYZ", "role": "runner", "pitch_hz": 600.0, "level_db": 0.0,
     "wpm": 28, "fist": {"dahdit_ratio": 3.0, "jitter": 0.05}, "qsb": null},
    {"call": "W2ABC", "role": "caller", "pitch_hz": 640.0, "level_db": -3.0,
     "wpm": 25, "fist": {"jitter": 0.10}, "qsb": {"period_s": 8, "depth_db": 10}}
  ],
  "messages": [
    {"t0": 12.34, "t1": 15.80, "call": "K1XYZ", "kind": "cq",       "text": "CQ CWT K1XYZ"},
    {"t0": 16.20, "t1": 17.60, "call": "W2ABC", "kind": "call",     "text": "W2ABC"},
    {"t0": 18.00, "t1": 21.10, "call": "K1XYZ", "kind": "exchange", "text": "W2ABC FRED 1234"},
    {"t0": 21.50, "t1": 24.00, "call": "W2ABC", "kind": "exchange", "text": "TU BOB VA"},
    {"t0": 24.30, "t1": 26.90, "call": "K1XYZ", "kind": "tu",       "text": "TU K1XYZ"}
  ],
  "qsos": [{"runner": "K1XYZ", "caller": "W2ABC", "t0": 12.34, "t1": 26.90, "completed": true}]
}
```
- `role`: `runner` | `caller` | `tailender` | `qrm` (unrelated station inside the cell).
- `kind`: `cq` | `call` (caller sends own call) | `exchange` | `tu` | `repeat` (AGN/?, partial
  repeats) | `other`.
- `pitch_hz` per station (constant); optional per-message `pitch_hz` if a station drifts.
- `level_db`: relative to the loudest station in the cell (0 dB). If stems are given,
  levels may instead be applied by the composer and `level_db` is the intended level.
- Timing (`t0`/`t1`, seconds from file start) is required for message-level scoring
  ("was this spot from a CQ?") but not for DeepFist training (CTC needs text only).

## Composer output (Spark Gap side)
`<scene>_<band>kHz_192000_24bit.wav` (I/Q, same as the skimmer1 recorder) +
`<scene>.truth.json`: every message with absolute `rf_hz`, `level_dbfs`, cell id —
scored by replaying through `hpsdr_proxy --wav` into SparkGap.
