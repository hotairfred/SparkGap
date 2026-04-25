# OpenSkimmer TODO

## Done (Apr 22-24 2026)
- [x] WPM + SNR reporting from ITILA decoder
- [x] CQ triggers aligned with CW Skimmer (CQ/TEST/QRZ/QRL/CWT/SST/MST/FD/SS/NA/UP)
- [x] DE token boundary fix (EC7RDE → EC7R + DE)
- [x] Text accumulation ("ticker tape") — 74% → 91% recall vs CW Skimmer
- [x] File mode auto-override for band limits
- [x] C receiver (libhpsdr_fast.so) — 9941 pkt/s, 8 DDCs × 192 kHz
- [x] C worker thread — autonomous drain + FIR + decode
- [x] C decode loop — ITILA decode in C, Python just polls results
- [x] Mutex for concurrent scanner access (worker + Python)
- [x] Multi-band (5 bands on bare metal i5-9500T)
- [x] RBN-validated scoring methodology
- [x] Relaxed callsign pattern for special event calls (YT170TESLA etc.)
- [x] Grid corrected to EM79

## Done (this session)
- [x] **FT8 live integration WORKING** (commit 4b6ca27 Apr 25 05:44 UTC).
      Replaced racy ring buffer with per-RX double-buffer + atomic swap.
      75-77 spots/cycle across 5 bands, zero UDP loss.
      DX: VK2 (Australia), TL8GD (CAR), TY5AD (Benin), ZL3 (NZ),
      OH3 (Finland), JA2 (Japan).
      See memory/feedback_ft8_double_buffer.md.

## In Progress
- [ ] **RTTY decoder** — Phase 1 core works (CQ CONTEST DE K3LR decodes on synthetic). Bit clock drift after ~15 chars. Needs wiring into live pipeline. Design doc on Atlas.

## Future
- [ ] **FT4 decoder** — ft8_lib handles FT4 too (already built); just needs second pipeline
  with -ft4 flag on 7s slots. Trivial to add once we want it.
- [ ] **RTTY Bayesian merging** — GRITTY-style multi-copy bit-level merge (Phase 2)
- [ ] **Auto mode detection** — detect FT8/RTTY/CW per channel from spectral signature
- [ ] **LNA gain control** — may not work with Pavel's sdr_receiver_hpsdr (all gains produce same output)
- [ ] **Proxy eval on localhost** — port binding collision, low priority
- [ ] **FIR chain optimization** — speed-adaptive filter width for different WPM

## Contest Schedule
- UK/EI DX Contest CW: 1200Z Apr 25 - 1200Z Apr 26 (80/40/20/15/10m)
- RTTY contest also this weekend — opportunity to test RTTY decoder
