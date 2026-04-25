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
- [x] **RTTY MVP wired into live** (commits 9f13b78 / 4e0a475 / ebac41c /
      bfbb9ea Apr 25 16:00-17:00 UTC). FT8-style minute snapshot,
      letter-first regex, multi-cycle confirmation, high-SNR shortcut,
      fuzzy variant collapsing. Decoded SN7Q, KD7ND, K0MK during SP DX
      RTTY contest.
- [x] **CW false-positive cleanup** (c565c06 / f27ab2b / 3976dcd Apr 25
      16:30-18:15 UTC). Dropped ITILA single-shot gate, bumped
      _min_sightings, disabled Path 1b sliding-window, added per-freq
      winner-takes-all filter. 87 OS-only FPs in 20 min → much smaller.
- [x] **CW worker per-RX parallelization** (commit f3a5640 Apr 25 18:50 UTC).
      Single worker was serializing all 5 RXs through ITILA decode →
      50% sample loss via ring overflow (415M drops / 8M packets).
      Spawned one worker per RX. Drops dropped to 2.3%, CW spot rate
      went 4 → 12.2/min on 20m, exceeds bare-C reference.
      See memory/feedback_cw_per_rx_workers.md.

## In Progress
- [ ] **Confirm Grayline contest comparison post-fix** — pull a fresh
      OS-vs-SDC compare after the per-RX worker fix to verify the EU/DX
      recall gap closed. Expected: 6 → many more overlap; SDC-only set
      shrinks substantially.

## Future
- [ ] **FT4 decoder** — ft8_lib handles FT4 too (already built); just needs second pipeline
  with -ft4 flag on 7s slots. Trivial to add once we want it.
- [ ] **Per-band parallel FT8 decode** — currently the minute-boundary
      FT8 decode runs serially across 5 bands × 46 sliding windows ×
      ~50ms each = ~12 sec compute compressed into one Python thread
      after each minute boundary. CPU spikes to ~80% (~4.8 cores) for
      that burst, well within budget. If burst length ever matters
      (e.g. for tighter spot latency or running more bands), spawn one
      decode thread per band — drops burst time to ~2.5 sec. Not urgent
      while we have headroom on 6 cores. Apr 25 2026.
- [ ] **Reduce remaining 2.3% ring drops** — per-RX workers cut drops
      from 50% to 2.3%, but still non-zero during heavy decode bursts.
      Options: larger ring buffers (currently 4 sec, could go 16+),
      separate decode thread from drain thread per RX, or smaller decode
      windows. Not urgent at current spot quality.
- [ ] **RTTY Bayesian merging** — GRITTY-style multi-copy bit-level merge (Phase 2)
- [ ] **Proper librtty_scanner.so** — RTTY MVP piggybacks on FT8 minute
      snapshot. Architecturally RTTY is continuous like CW; should have
      its own PFB-channelized scanner with persistent per-channel
      decoder state (matches ITILA pattern). MVP works for strong contest
      signals; proper scanner would close the weak-signal gap.
- [ ] **Auto mode detection** — detect FT8/RTTY/CW per channel from spectral signature
- [ ] **LNA gain control** — may not work with Pavel's sdr_receiver_hpsdr (all gains produce same output)
- [ ] **Proxy eval on localhost** — port binding collision, low priority
- [ ] **FIR chain optimization** — speed-adaptive filter width for different WPM
- [ ] **Watch list first-hear semantics** — wire add_calls.txt so listed
      callsigns bypass sighting threshold and spot on first hear (beacons
      like AG8Y/B, known rare DX). Currently parsed but not really used.
- [ ] **CQ flag in telnet output** — RBN parses a `CQ` marker; we have
      `_cq_seen` tracking internally, just need to append to spot line.

## Contest Schedule
- UK/EI DX Contest CW: 1200Z Apr 25 - 1200Z Apr 26 (80/40/20/15/10m)
- RTTY contest also this weekend — opportunity to test RTTY decoder
