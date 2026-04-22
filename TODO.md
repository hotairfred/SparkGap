# OpenSkimmer TODO

## Decoder

- [x] **Token boundary parsing** — fixed DE suffix splitting (`EC7RDE` → `EC7R` + `DE`). Preserves real DE-prefix calls like `DE5FHI`.

## Channelizer / Scanner

- [x] **SNR passthrough** — per-bin SNR from FFT scan stored in C scanner, updated each rescan, wired through to spots.

- [x] **File mode fixed** — was not a code bug. Config band limits (20m) didn't match recording (40m). Added auto-override: file mode detects when center_khz is outside cw_min/cw_max and adjusts. 15 min recording processes in ~2m47s.

- [ ] **Proxy eval on localhost** — port binding collision on localhost. Low priority since file mode works now.

## Infrastructure

- [x] **CQ triggers aligned with CW Skimmer** — CQ, TEST, QRZ, QRL, CWT, SST, MST, FD, SS, NA, UP. Removed TU (not a CQ keyword, spots worked stations). Removed DE (too noisy).

- [x] **WPM reporting** — `itila_get_wpm()` added to C library, wired through scanner → spot output. Live spots now show decoder-estimated WPM.

- [x] **Grid corrected** — EM79 in all configs (was EN82).

- [ ] **RBN daily comparison** — `rbn_pull.sh` + `rbn_compare.py` ready. Awaiting 20260422.zip for definitive CWT validation. Cron set for daily pull at 0617 local.

- [x] **Push to GitHub via Grayline** — done.
