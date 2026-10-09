<!-- markdownlint-disable MD013 -->
# itila2: design and rationale

itila2 is a second CW decoder chain for SparkGap, kept beside the original itila chain so the two can be compared on the same audio. It shares itila's architecture (wideband IQ, a scanner that spawns one channel per signal, an HMM decoder per channel) and changes the parts that measurement showed to be limiting. This note describes how itila2 works, what differs from itila, and the evidence behind each difference.

Status: the decoder and scanner are in this repository; the spot rule (`spot_rule: repeat`) is a separate pull request. Development is tracked in `cdub89/SparkGap#8`.

## Selecting it

| Config key | Value | Effect |
| --- | --- | --- |
| `cw_decoder` | `itila` (default) | `itila_core.c` + `itila_scanner.c`, Fred's chain, unchanged |
| | `itila2` | `itila2_core.c` + `itila2_scanner.c` (`libitila2.so`, `libitila2_scanner.so`) |
| `itila_decode_threads` | 1 (default) | number of bins decoded in parallel, and scanner DSP worker threads |

Decoder and scanner are selected together: an itila2 decoder on itila's scanner (or the reverse) is never run. Both chains use the same spot path.

## Signal chain

```text
SDR IQ (Flex DAX-IQ 48/96 kHz, HPSDR, or a WAV)
  -> scanner: FFT scan (4096 points), CFAR peaks, spawn one bin per signal
  -> per bin: mix to baseband, stage-1 FIR to 12 kHz, stage-2 FIRs to 2 kHz
     on two paths (narrow and wide), magnitude, stage-3 FIR to 200 Hz envelope
  -> decoder, per 60 s window per path:
     level reference, EM-fitted two-state HMM, forward-backward posterior,
     marks and spaces, despeckle, timing fit, beam search over Morse
  -> decoded text per window
  -> spot path -> telnet DX spots
```

## Scanner (`itila2_scanner.c` against `itila_scanner.c`)

| | itila | itila2 | Why, and evidence |
| --- | --- | --- | --- |
| FIR inner loop | modulo per tap | two straight loops, same taps in the same order (SC2) | Every decoded line identical on all recordings; CWT CPU 0.57 to 0.41 x real time |
| Sample counters | `int`, overflow after about 6 h at 96 kHz (3 h at 192) | wrapped at common multiples, time counters `int64_t` (SC3) | Out-of-bounds writes after hours of running. Also in itila via `hotairfred/SparkGap#8` |
| Stage-1 FIR | about 13 dB rejection at the 12/24/36 kHz fold bands | longer FIRs, 40 dB or more at the folds (SC9) | Strong stations were decoded and spotted 12 kHz away; alias pairs 230 to 6 on B1. Also in itila via `hotairfred/SparkGap#8` |
| Narrow path | magnitude of the stage-2 output | 40 ms Hann window before the magnitude (SC5) | Sharper key-state on the narrow path; adopted in `e2a0958` |
| Wide path | stage-2 FIR to 200 Hz only: -2 dB at 200 to 300 Hz, -14 dB at 400 to 500 Hz | channel filter after stage 2: flat to +-75 Hz, -60 dB by +-150 Hz | Neighbours leaked into the wide path; the narrow path alone loses fast CW. 40m CWT shared with CW Skimmer 77 to 76, spots it never sent 30 to 21; 20m: calls both CW Skimmers decoded 75 to 76, SparkGap-only call strings 483 to 448; W1AW unchanged; CPU +3 to +9% |
| New bin start | first element often lost | 1 s of IQ replayed into a new bin (SC7) | +4 calls on the fixed set |
| Bin spacing | peaks snapped to a 50 Hz grid; a new bin blocked only when closer than 50 Hz, so the next grid point of the same signal always spawned | spacing 75 Hz measured on the interpolated peak frequency; the bin centred on the average of its candidate's peaks within 25 Hz (SC11) | One strong station took 2 to 5 bins: wasted CPU, and one transmission counted as several windows by the spot rule. CPU -41% (20m 1 h) and -43% (20m CWT); CWT spots CW Skimmer never heard 16 to 5. Co-channel stations 100 Hz apart (K1GU next to WJ9B) stay separate |
| Bin lifetime | spot evidence only | spot evidence only | A word keep-alive was tried and removed: noise decodes (ES, IS, E5E) kept 170 of 219 bins alive and memory only grew |
| Threads | one | per-bin DSP split over a worker pool (`itila_decode_threads`) | Main thread 99% to 15%; 4 min replay 101 s to 59 s, identical decodes |

## Decoder (`itila2_core.c` against `itila_core.c`)

Shared with itila: the 200 Hz envelope, a two-state HMM (mark, space) whose parameters EM fits per window, speed marginalised over 16 WPM bins, forward-backward posterior, run lengths, beam search over the Morse tree, callsign extraction. itila2 searches speeds up to 35 WPM: noisy windows pin the speed estimate at whatever the cap is (12 to 17% of windows at 35, 38, 40 or 45), and a sweep of those four caps on three recordings found 35 best by a small margin (fewer busted calls, one character less W1AW error). The speed reported with a spot is measured separately (see below), so it is not limited by the cap.

| | itila | itila2 | Why, and evidence |
| --- | --- | --- | --- |
| Level reference | the whole window divided by its 99th percentile: one mark level per 60 s | a two-way peak follower (0.2 s decay) gives each element the level of its neighbourhood; the floor is the larger of 10% of the window's range and 8x its noise; 55% of the local peak maps to full mark level (DC5) | W1AW's element level moves 3 to 4 times within seconds, so a window-wide level dropped and split weak elements. Known-text character error on W1AW 42/54/34% to 3.1/3.7/1.9% on three clips (one never used for tuning). The noise floor keeps steady signals intact: without it, 20m recall halved because noise between transmissions was stretched into text |
| Carry | each window decoded on its own | the unfinished last word (up to 20 s) is carried raw into the next window and normalised together with it | Words cut at window boundaries |
| Sub-dot runs | every run is an element or a gap | runs shorter than 0.4 dot (rounded) are absorbed into their neighbours, shortest first (DC6) | A 25 ms dip split a dah into a dah and a dit (ON read YN). Rounding up instead cost 3 confirmed CWT spots |
| Dit unit for spaces | from the EM speed estimate | fitted from the window's own mark runs (C2) | The EM unit was often wrong on clean keying (11.3 samples for a true 13), so every gap threshold was mis-scaled. +1 to +2 calls on each of four recordings, nothing lost |
| Dit and dah for marks | 1 and 3 units of the EM speed | fitted dit and dah from the marks, CWReader-style (C7); a second pass when dahs hide the dits (C10) | +1 held-out call, nothing lost |
| Letter/word gap boundary | fixed 5 units | fitted from the window's gaps, idle time excluded, with a class-separation guard (C1) | Operators whose letter gaps run 4 to 5 units were split into extra letters (W6TED) |
| Per-bin memory | none | last fitted dit and dah kept per handle, used when a window has too few marks to fit (DC1) | Short windows no longer fall back to a wrong unit |
| Reported WPM | EM speed estimate | median of mark plus following element gap (2 units after a dit, 4 after a dah); the EM estimate when a window has fewer than 8 | The level decision lengthens marks and shortens gaps by the same amount, so marks alone read slow: 40m CWT spots were 0.79x CW Skimmer's WPM, now 1.00x, 112 of 129 within 3 WPM. Decoding is unchanged |
| Noise inside a window | a window that passes the evidence test is decoded whole | keying between pauses of 5 units (fastest speed) is scored alone against noise; segments under `SEG_RATE_MIN` (300 log BF per second) are blanked before the beam | Noise stretches in a passing window decoded as E, I, S, 5 strings. 20m h2h: SparkGap-only call strings 448 to 169, dit noise 200 to 6, calls both CW Skimmers decoded 76 to 77; WX7V/5 calls held on 40m CWT (77), 20m 10-06 (8), 20m CWT 09-23 (49); W1AW unchanged; log -30 to -60%; CPU +6 to +9% |
| Decode buffers | per handle, about 1.6 MB each (two handles per bin) | one set per decode thread; a handle keeps only its carry and fitted values | Memory grew with every bin on a busy band. 200 handles 325 MB to 8.5 MB; replay peak 2,055 to 1,202 MB on the 40m CWT; every decoded line identical on the 20m hour, 40m CWT and W1AW |
| Window test order | evidence (16-speed forward-backward) then separation | separation first; the forward-backward runs only when separation passes | A window under SEP_MIN fails either way (1,742 of 1,745 failures on the 20m hour). Identical output; CPU -6% on the 20m hour, -8% on a quiet 17m band |

## Spot rule

The spot rule written for itila2 (`spot_rule.py`, `spot_rule: repeat`: CQ/TEST, repeats by call pattern as CW Skimmer validates, copies merged into the call heard on the frequency) is a separate pull request. Without it both chains use the existing spot path.

## How it is measured

| Measure | Source | Used for |
| --- | --- | --- |
| Character error against known text | W1AW code practice and bulletins (CW Skimmer copies them nearly perfectly); clips and harness in the bench folder | Decoder changes: tune on one clip, check on held-out clips |
| Spots shared with the reference | WX7V/5 Aggregator log (CQ spots sent to RBN) on the same radio | Spot-level recall at the Lakehouse |
| Confirmed | `tools/eval/rbn_confirm.py`: another RBN skimmer heard the call within 1 kHz | Precision; WX7V/5's own spots excluded |
| CPU and memory | `/usr/bin/time` on replays; bins and RSS on the status line live | Cost; every change must be CPU-neutral or cheaper |

Call-count scoring alone (5 to 33 calls per recording) could not separate real gains from noise; the known-text measure is what made DC5 and DC6 visible.

## Results

File-mode replays, both chains with the default spot path (no spot rule), `itila_decode_threads` 4 for itila2. Reference: WX7V/5, the Lakehouse CW Skimmer on the same FLEX-6600 (its Aggregator log of CQ spots sent, or its telnet output for the 09-23 CWT).

| Recording | itila | itila2 |
| --- | --- | --- |
| W1AW clips A / B / C, character error vs true text | no bin on the W1AW tone (itila2 decoder on itila's scanner: 49.5 / 69.4 / 34.0%) | 3.1 / 3.7 / 1.9% |
| 40m CWT 2026-10-08, 1 h: WX7V/5 calls shared / calls WX7V/5 did not send | 76 of 91 / 263 | 81 of 91 / 171 |
| 20m 2026-10-06, 1 h: WX7V/5 calls shared / calls WX7V/5 did not send | 4 of 17 / 15 | 6 of 17 / 26 |
| 20m CWT 2026-09-23, 26 min: CW Skimmer CQ calls shared | 46 of 57 | 49 of 57 |
| CPU time, the three replays | 3257 / 1523 / 756 s | 1797 / 1027 / 459 s (-33 to -45%) |

On a live 40m CWT hour (2026-10-08, FLEX-6600, itila2 with the spot rule) no IQ was lost or trimmed and bins peaked at 184 with 1.1 GB.

Where both itila2 and CW Skimmer misread W1AW, the received element itself is cut (a dah reads short or a dit is missing): RUN TO BE, EDT, THURSDAY, FROM, W1AW. On a hand-keyed 40m net both produce nearly the same text, oddities included.

## Tried and rejected

| Change | Result |
| --- | --- |
| Fade gain applied to the fitted mark level after EM (C3 to C5) | Damaged steady signals, or did nothing once tamed |
| 30 Hz matched envelope channel (C6) | Rounded mark edges; fewer spots |
| Four times longer scan FFT (C8) | Separated close carriers but lost calls |
| Mark level per segment after the gate (C9) | Fewer calls |
| Per-state variances, soft decisions (DC2, DC3) | No gain |
| Window continuity alone (DC4) | No gain |
| Word keep-alive (SC6, SC10) | Noise kept bins alive; memory grew |
| Peak-prominence mask at spawn | No gain beyond SC11 |
| Level reference with a range-only floor | Halved 20m recall (noise stretched into text) |

## Future work

### Web-888 and HPSDR multi-band, alongside the Flex

itila2 has been developed and measured on one band from a Flex (DAX-IQ, 96 kHz). The next platform is the Web-888 SDR receiver, which SparkGap reaches through HPSDR protocol 1: the multi-band C receiver path (`hpsdr_fast.c`), the same path Fred runs in production (`sk_5band.json`, 8 bands). The Flex path stays; the two are complementary (a Flex operator skimming one band on a spare panadapter, a Web-888 skimming several bands at once).

What carries over: the decoder changes (DC5, DC6, C1 to C10) live in `itila_feed` and run unchanged on the C path; the scanner changes (SC2, SC3, SC9, SC11) and the scanner worker pool are inside the scanner library both paths share. The spot rule applies to any decoded window.

What needs checking or work before itila2 can run there unattended:

| Item | Why it matters |
| --- | --- |
| itila2 on the C decode path, measured | So far only the Flex and file paths are measured; the C path decodes inside the scanner's worker thread |
| Multi-band poll loop never marks bin evidence (`cdub89/SparkGap#5` L2) | Bins with real stations are evicted and respotting breaks |
| No exception isolation in the main poll loop (L4) | One bad result stops the daemon |
| HPSDR packet loss never counted; `hpsdr_stop` can hang; IQ ring without memory barriers | Silent audio loss, and failures on ARM single-board computers |
| CPU across bands: Fred's 8-band replay saturates at about 1,470 bins (1.5 M envelope drops in 6.5 min) | SC11 removes about a third of the bins; Fred has offered to run his 8-band test on it |
| Replay tools: `hpsdr_proxy` multi-receiver WAV replay (`hotairfred/SparkGap#12`) and the all-band 24-bit recorder (`hotairfred/SparkGap#11`) | A Web-888 capture can then be replayed band for band, as the Flex recordings are today |
| A reference for each band | The Lakehouse comparison uses one CW Skimmer on one band; a multi-band reference has to be chosen |

Order: a single band from the Web-888 first, scored the same way as the Flex runs; then the bands Fred runs; then unattended operation.

### Other

- Hand-keyed fists (bugs, sideswipers, straight keys): fit the timing per transmission instead of per 60 s window; a daily hand-keyed 40m net and the operator's own sideswiper make known-text test material.
- Windows support (`cdub89/SparkGap#5`): most RBN nodes run Windows; the native libraries need Windows builds.

## Open

- Hand-keyed fists (bugs, sideswipers, straight keys): dah lengths vary, and a net puts several fists in one 60 s window. Next candidate: fit the timing per transmission instead of per window.
- `itila_feed_online` has the level reference but is not used by SparkGap.
