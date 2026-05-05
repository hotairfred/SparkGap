# SparkGap TODO

_Updated 2026-04-29. Completed work lives in `git log`._

## Now

- [ ] Multi-day data collection via `score_loop.py`
- [ ] Build empirical blacklist from persistent solo-noise calls
- [ ] Re-introduce `gate_patt3ch_filter` once a baseline is documented
- [ ] Email N4ZR for supervised RBN test (draft at
      `/home/fred/email_n4zr_draft.md`, outside repo)
- [ ] Go live on production RBN once N4ZR signs off

## Soon

- [ ] **Watch-list semantics** — wire `add_calls.txt` so listed calls
      bypass the sighting threshold (beacons like AG8Y/B, known rare DX)
- [ ] **CQ flag in telnet output** — RBN parses a `CQ` marker; we have
      `_cq_seen` internally, just need to append to the spot line
- [ ] **Split `enable_ft8` / `enable_rtty`** config flags ... RTTY
      currently piggybacks on `enable_ft8`

## Backlog

- [ ] **FT4 decoder** — `ft8_lib` already builds it; second pipeline
      with `-ft4` flag on 7s slots
- [ ] **Per-band parallel FT8 decode** — minute-boundary CPU spike;
      not urgent on 6 cores
- [ ] **Reduce 2.3% ring drops** under heavy decode bursts
- [ ] **RTTY Bayesian merging** — GRITTY-style multi-copy bit-level
      merge (Phase 2 of RTTY work)
- [ ] **Proper `librtty_scanner.so`** — RTTY MVP piggybacks on FT8
      minute snapshot; should have its own PFB-channelized scanner
- [ ] **Auto mode detection** per channel from spectral signature
- [ ] **`IqSource` plugin interface** — clean abstraction so RX-888 /
      SDRPlay / Hermes Lite 2 backends become community-contributable
