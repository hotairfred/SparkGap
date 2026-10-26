# Voice (SSB) spotting

SparkGap's voice skimmer finds SSB voices in the IQ, transcribes each one with Whisper and spots
the station calling CQ, the same way the CW repeat rule does: the runner, never the caller. This
folder holds it as it lands, piece by piece:

| Part | File | State |
| --- | --- | --- |
| Spot rules: transcripts in, spots out | `voice_spot.py` | here |
| Active US calls from the FCC database | `fcc_calls.py` | here |
| Voice detector: IQ in, one audio track per voice | | next |
| Live runner: detector, Whisper and the rules on a running radio, spots on telnet | | after that |

## What the rules do

Whisper gives text like `CQ contest, CQ contest, Whiskey Foxtrot 8 Zulu, contest`. The rules
read phonetics (NATO and the usual alternates such as Kilowatt, Sugar, Radio, Italy, Japan),
digits ("four", "for", "oh", "forty", "59"), and portable suffixes into calls, and then decide who
is the runner:

- **A runner copy** is a call right after CQ, QRZ or "this is", or one right before "QRZ",
  "contest" or "calling CQ", with a CQ or QRZ in the last 30 s. A call right after "thank you",
  "copy", "you're" or a report is a caller and never counts.
- **Two copies spot a call**, within 500 Hz and 10 minutes. 1x1 calls, calls longer than a 2x3 and
  calls in neither MASTER.SCP nor the FCC list need three: live, a misreading is often heard before
  the real call has its copies (XR6K before CR6K, P4X before IP4X), as RBN's pattern file allows for
  in CW. A US-format call must be in the FCC list (`--fcc`, built by `fcc_calls.py`).
- **The better of two readings wins.** When a variant of the call is heard on the same carrier
  (I8R beside HI8R, MS53M beside S53M, 1B1B beside CB1B), the known call wins (MASTER.SCP, or the
  FCC list for a US call), then a call that only adds letters in front, then more copies.
- **The dominant call.** Contest runners mostly name the caller ("K3NM, 59 05") and sign only now
  and then, so a known call named at least 4 times in 10 minutes on one carrier, 3 times as often
  as any other and with contest words about, is spotted as that carrier's runner.
- **The answer to a question is a caller.** After "again?", "your call?" or "say again", the next
  call heard is someone being asked for their call.
- **One spot per call within 3 kHz.** One voice can show up as two or three carriers a kHz or two
  apart; it is spotted once, on the carrier with the most copies.
- **Lock-on.** A call heard on a carrier that another station is running (that station heard in
  the 330 s before, with twice the copies around it) is a caller answering the runner, not
  another runner. A station that takes over a frequency someone has left is spotted as usual.

Each rule can be turned off for measuring with an environment variable: `VS_VARIANTS`,
`VS_DOMINANT`, `VS_ASKS`, `VS_LOCKON` (`0` = off), `VS_SAME_KHZ` (kHz, `0` = off),
`VS_VARIANT_S` and `VS_POOL_S` (seconds), `VS_UNKNOWN_COPIES` (copies for a call on neither list),
`VS_SCP` (a newer MASTER.SCP).

## Running it

```
python3 voice/fcc_calls.py l_amat.zip > fcc_active_calls.txt     # weekly FCC file, see the script
python3 voice/voice_spot.py DUMPDIR large-v3-turbo --strict --fcc fcc_active_calls.txt [--debug]
```

`DUMPDIR` holds one Whisper transcript per voice track,
`ssb_<carrier kHz>@<start s>.<model>.json`, each a list of
`[start, end, text, avg_logprob, [[word, probability], ...]]`. The detector that writes those comes
in the next pull request. `--debug` prints why each heard call was or wasn't spotted.
Tests: `python3 -m pytest tests/test_voice_spot.py`.

## Measured

CQ WW SSB 2026: eight band-hours recorded from a FlexRadio (20, 40, 15 and 80 m), transcribed
with faster-whisper `large-v3-turbo` (int8) on a GTX 1060, and scored against the DX cluster spots
people posted (DX Summit and a cluster node) for the same call within 3 kHz and 30 minutes:

| | Spots | Calls | Calls a person also spotted |
| --- | --- | --- | --- |
| These rules, all eight band-hours | 368 | 349 | 248 |

- "Not spotted by a person" isn't the same as wrong: in CQ WW almost nobody spots US and Canadian
  stations, and most of our unconfirmed calls are those. Reading the transcripts of two of the
  blocks, about 91% of our spots were real.
- The variant, dominant-call and question rules were written on the first two band-hours and then
  checked on two held-out ones: confirmed calls on 20 m went from 20 to 26 and on 15 m from 46 to
  50, and 8 busts went away.
- One spot per call within 3 kHz and lock-on together removed 27 duplicate or caller spots from
  the eight band-hours, and three copies for calls on neither list 4 misreadings (TK1TTT, IR3Z,
  S5T, T9FRX); no call a person had spotted was lost.
- The same rules in the live runner, on one 40 m hour replayed with nothing dropped: 67 calls,
  54 of them spotted by a person (81%), against 71 and 54 (76%) before these three changes.

Run live on one band, the GTX 1060 falls about a third short of keeping up with a busy contest
band, so a busy hour loses some speech it never gets to. That is for the live-runner pull request.
