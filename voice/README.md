# Voice (SSB) spotting

SparkGap's voice skimmer finds SSB voices in the IQ, transcribes each one with Whisper and spots
the station calling CQ, the same way the CW repeat rule does: the runner, never the caller. This
folder holds it as it lands, piece by piece:

| Part | File | State |
| --- | --- | --- |
| Spot rules: transcripts in, spots out | `voice_spot.py` | here |
| Active US calls from the FCC database | `fcc_calls.py` | here |
| Voice detector: IQ in, one audio track per voice | `voice_detect.py` | here |
| FlexRadio DAX-IQ recorder (one SparkGap station on the radio) | `flex_record.py` | here |
| Live runner: detector, Whisper and the rules on a running radio, spots on telnet | `live_voice.py` | here |
| Live rules on saved transcripts, without the GPU | `live_rule_replay.py` | here |

## How voices are found

`voice_detect.py` looks at the band every 2 s (25 Hz bins, 40 ms frames) for the shape an SSB voice leaves:

- **Shape:** for each candidate carrier (50 Hz steps), the 2.3 kHz voice window on one side (300 to
  2600 Hz from the carrier) averages at least 6 dB over the noise floor and 4 dB over the carrier side,
  which a transmitter's filter leaves quiet. LSB below 10 MHz, USB above and on 60 m.
- **Spread:** at least 30% of the window's bins are 6 dB over the noise floor. A CW carrier or a birdie
  fills one bin.
- **Rhythm:** the window's loudness, frame by frame over the 2 s, varies by at least 3 dB (standard
  deviation): syllables and pauses. Carriers, RTTY, FT8 and noise stay flat.
- **No carrier:** a peak 10 dB over the window at the carrier itself is AM or a carrier, not SSB.
- **One voice per 2.2 kHz** of a sideband in each window, the strongest.
- **Tracks:** a hit within 400 Hz of a live track extends it, and a track ends after 120 s without one.
  It is kept with 3 hits or more, or with 2 within 10 s: a sporadic voice counts (YO7WC on 15 m in CQ WW
  came only from 3 hits minutes apart). Each track's audio is cut at 16 kHz on its sideband, in bounded
  memory (one FFT per minute of IQ, every block the same length so the audio has no step at the joins),
  for the transcriber.

```
python3 voice/voice_detect.py IQ.cf32 CENTRE_KHZ LO_KHZ HI_KHZ OUTDIR [--start S] [--dur S] [--tracks-only]
```

IQ is 192 kHz: `.cf32` (interleaved float32 I, Q) as measured on FlexRadio DAX-IQ, or a stereo 16- or
24-bit WAV as `record_wav` and `iq_recorder.py` write them (read, but not yet measured on voice; a Red
Pitaya recording may need Q negated, as `hpsdr_proxy.py --negate-q` does). OUTDIR gets `tracks.json`
(the span, every hit and track) and `clips/ssb_<carrier kHz>@<start s>.wav`. Thresholds: `VD_SHAPE_DB`,
`VD_LEVEL_DB`, `VD_SPREAD`, `VD_RHYTHM_DB`, `VD_END_S`. `VD_TMP`: where the cutter keeps its temp files
(default OUTDIR, since /tmp can be RAM; removed even on an error). `VD_REUSE=1`: cut from an existing `tracks.json` of the same
span instead of detecting again. Tests: `tests/test_voice_detect.py` (synthetic voices, a CW carrier, a
data signal, the track rules, the cutter's audio and its phase across block joins at an awkward carrier,
tracks at the ends of a file, cleanup when a step fails, and 16- and 24-bit WAV).

Known limits, from the CQ WW recordings:

- **The carrier estimate sits 0 to 300 Hz on the voice's quiet side** (the live runner moves its spots
  100 Hz back), and it can jump half a kHz or more when a station changes operators or voice. A
  voice then makes two or three tracks a kHz or two apart: 24 to 42% of track time on busy bands is
  such a second track, transcribed again. The spot rules spot each call once within 3 kHz, so it
  costs transcription time, not spots. Three fixes were measured and dropped:
  - joining within 1.1 kHz saved 13 to 26% of transcription but merged real neighbours 1 to 2 kHz
    apart and lost 14 confirmed calls;
  - letting a second voice closer than 2.2 kHz through when its own slice has speech that doesn't
    follow the stronger voice gained no calls and cost 45% more transcription;
  - comparing the cut audio of overlapping tracks can't tell the cases apart: tracks a kHz apart
    share most of their window, so their loudness follows whichever voice is loudest.
- **A very strong voice can leave a weak hit about 2.5 kHz to its quiet side** (spectral leakage);
  rare on contest recordings (0 to 1.5% of track time).

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
  the 330 s before, with twice the copies around it, or as many once it already had two before)
  is a caller answering the runner, not another runner. A station that takes over a frequency
  someone has left is spotted as usual.

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
`[start, end, text, avg_logprob, [[word, probability], ...]]` (the clips come from `voice_detect.py`).
`--debug` prints why each heard call was or wasn't spotted. Tests: `python3 -m pytest tests/test_voice_spot.py`.

## Running live

The live runner needs faster-whisper and a CUDA GPU: `pip install -r voice/requirements.txt`, and put
the pip CUDA libraries on the path:

```
export LD_LIBRARY_PATH=$(python3 -c "import os, nvidia.cublas as b, nvidia.cudnn as d; print(':'.join(os.path.join(list(m.__path__)[0], 'lib') for m in (b, d)))")
```

Record a band from a FlexRadio and run the skimmer on the recording as it grows:

```
python3 voice/flex_record.py 192.168.1.50 rec 3600 auto:14246000 &
python3 voice/live_voice.py rec/*_14246kHz_*.cf32 14246 14153 14339 --follow --dur 3600 \
        --call N0CALL-4 --port 7373 --fcc fcc_active_calls.txt --spots spots.txt
```

- Spots go out on the telnet port as `DX de N0CALL-4: 14246.0  K1ABC  SSB CQ 2 copies  1234Z`, for a
  cluster or a logger. Give the skimmer an SSID of its own.
- A 192 kHz DAX-IQ stream takes half the radio's DAX-IQ budget, so a radio records two bands at most.
- Without `--follow` it replays a recording: `--speed 1` at live pace (the keep-up test), `--speed 0` as
  fast as it goes.
- Transcripts stay in memory (15 minutes) and are dropped. `--transcripts DIR` keeps them, appended to
  `DIR/segs.jsonl`, and `live_rule_replay.py DIR/segs.jsonl START_UTC` replays the live rules on them in
  seconds, without the GPU, to measure a rule change.
- When the GPU falls behind, a piece more than 10 minutes behind the newest audio is dropped and its
  frequency gets the next fresh look. A spotted runner's frequency is only sampled (30 s every 5 minutes)
  until it goes quiet. If 20 pieces in a row fail to transcribe (a missing CUDA library, say), the runner
  stops with `FATAL` instead of running on without spotting.

Tests: `python3 -m pytest tests/test_live_voice.py` (the live rules, the telnet port, the failure stop and
each track's audio across blocks; no GPU or radio needed).

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

**Live, into a DX cluster**, CQ WW SSB 2026, Sunday 06:55 to 23:58Z on one band (40 m, then 20 m):
2083 spots of 412 calls, 203 of the calls also spotted by a person (49%; again, mostly US and Canadian
stations nobody spots). Most spots went out within seconds of the station being heard.

**One GTX 1060 is about a third short of one busy contest band.** On 20 m on Sunday the runner dropped 300
to 1500 pieces an hour that it never got to. A 40 m hour replayed at 0.6x speed, so that nothing was
dropped, gave 71 calls with 54 confirmed, against 64 and 53 live with the same rules: the drops cost
few confirmed calls, but late spots and missed ones. A faster GPU, or two, is the fix.
