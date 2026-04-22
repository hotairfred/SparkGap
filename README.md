# OpenSkimmer

Open source Linux CW skimmer with ITILA Bayesian decoder. Spots CW stations from wideband IQ and serves them on a DX cluster telnet port.

## Architecture

```
SDR (HPSDR Protocol 1) → 192 kHz IQ
  → FFT energy scan (signal detection)
  → Per-bin channelization (mix + decimate → 12 kHz)
  → Dual-path FIR envelope (150 Hz + 300 Hz linear-phase)
  → ITILA Bayesian decoder (HMM forward-backward + EM)
  → Callsign extraction + SCP validation
  → SpotTracker (sighting accumulation, Morse weight scoring)
  → DX cluster telnet output
```

Based on MacKay's *Information Theory, Inference, and Learning Algorithms* (2003). CW modeled as a two-state HMM with Gaussian mixture observation model.

## Hardware

- **SDR:** Red Pitaya STEMlab 125-14 or any HPSDR Protocol 1 device
- **Antenna:** Any HF antenna
- **Computer:** Linux x86_64 (tested on 4 cores, 2 GB RAM)

## Build

```bash
sudo apt install gcc python3 python3-numpy python3-scipy
make
```

## Quick Start

1. Copy `skimmer_example.json` → `skimmer.json`, edit callsign/IP/bands
2. Download `MASTER.SCP` from [supercheckpartial.com](http://www.supercheckpartial.com/)
3. `python3 openskimmer.py --config skimmer.json`
4. `telnet localhost 7300`

## Benchmark

B1 40m CWT, 15-minute segment, 56-call CQ-only key:

| Mode | Score | Recall | Precision |
|---|---|---|---|
| Proxy (real-time, FIR) | 43/56 | 76.8% | 90%+ |
| Proxy (real-time, IIR) | 48/56 | 85.7% | 94% |
| File mode | 47/56 | 83.9% | — |

FIR linear-phase chain is the production default. CY0S pile-up signal decoded for the first time with FIR — IIR phase smearing was the root cause of "impossible" decode failures. Score expected to improve with extraction tuning on the FIR envelope.

## WAV Replay

```bash
python3 hpsdr_proxy.py --wav recording.wav --negate-q   # terminal 1
python3 openskimmer.py --config skimmer.json             # terminal 2
```

## License

GPL-3.0

## Credits

- ITILA framework: MacKay (Cambridge, 2003)
- Inspired by VE3NEA's CW Skimmer
- uhsdr decoder: UHSDR project (DF8OE et al.)
