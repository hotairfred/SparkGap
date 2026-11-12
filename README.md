# SparkGap

*(Formerly OpenSkimmer; not related to Karl Heinz Kremer's OpenSkimmer of 2026, nor to SparkSDR by
M0NNB.)*

Open-source, Linux-native, multi-band skimmer for amateur radio: **CW, RTTY and FT8** on
several bands at once, with DX-cluster spots on telnet, an uploader for the Reverse Beacon
Network (RBN) and FT8 reports to PSKReporter. **SSB voice** spotting is in `voice/` too
(experimental; it needs a CUDA GPU).

> **Status: beta, live on RBN.** Two beta nodes upload to RBN with `rbn2_feeder.py`: WF8Z-1 (live
> since 2026-11-05; since 11-10 on an RX888 through ka9q-radio, 9 bands from 160 to 10 m) and WX7V-1
> (a Web-888).
> Anyone else: run with `rbn_upload` off until your node holds up against other skimmers
> (*Feeding a cluster or RBN* below).

## Where it stands

Scored per spot against the RBN archive: a spot is confirmed when another skimmer spotted the same
call within 1 kHz and 10 minutes.

- **Precision, live.** WF8Z-1's first full day on the RX888 (2026-11-11): 95.5% of its CW spots
  confirmed. The same day replayed through the spot rule with the two options turned on since
  (`spot_rule_owned_two_windows`, `rbn_known_drop_busts`): 97.3%. WX7V-1 in the 11-11 19Z CWT:
  98.7% of 611 spots. CW Skimmer nodes run about 95 to 98% on the same measure. Most of what isn't
  confirmed is either a real station nobody else heard or a caller read in a runner's text; the
  options above and `itila2_tone_lock_hz` target the second.
- **Recall.** Against a CW Skimmer node 20 miles away, on the same evenings in October (Red Pitaya
  receiver), SparkGap spotted 1.6 to 1.9 times as many calls and caught about 70% of that node's
  (#43).
- **RTTY.** One hour of the PA QSO Party and Makrothen weekend (10-10 22Z), the same IQ replayed to
  SparkGap and RTTY Skimmer Server, both on a Makrothen profile: RTTY Skimmer Server tagged 116
  stations CQ, 110 confirmed; SparkGap spotted 93, all confirmed, with the default RTTY decoder, and
  106, 105 confirmed, with `"rtty_decoder": "frtty"`. Details in #91.
- **SSB (voice).** CQ WW SSB 2026, eight band-hours recorded from a FlexRadio and scored against the
  DX cluster spots people posted: 347 calls spotted, 248 of them also spotted by a person (almost nobody
  spots US and Canadian stations in CQ WW, and most of the rest are those); reading the transcripts,
  about 91% of spots were real. Live into a cluster for Sunday of that weekend: 2083 spots of 412 calls,
  most within seconds of the station being heard. One GTX 1060 is about a third short of one busy
  contest band. Details in `voice/README.md`.
- **Copy on known text.** Character error on three W1AW code-practice clips: 3.1, 3.7 and
  1.9% (`docs/ITILA2.md`; CW Skimmer copies the same audio nearly perfectly).
- **Cost.** 8 bands at 192 kHz with CW, FT8 and RTTY use about 1.7 cores of CPU time on a 35 W
  i5-9500T with `itila_decode_threads: 4` on a weekday afternoon (2.2 before the October CPU work;
  profile in #59). On an RX888, `radiod` takes about half a core more for 9 bands.

The CW, RTTY and FT8 numbers are calibration on our stations, not contest results; the SSB ones are
from one contest weekend. The harness to reproduce them is in `tools/eval/` and `tools/sim/`.

## What it does

- **Receivers.**
  - **RX888 or any receiver ka9q-radio's `radiod` drives** (`"sdr_type": "rtp"`, `rtp_receiver.py`):
    `radiod` digitises the whole of HF at once and hands SparkGap one IQ channel per band, so a band
    is a line in `radiod`'s config, not more hardware. The cheapest way in, and what WF8Z-1 runs.
  - **HPSDR protocol 1 radios**: a Red Pitaya with Pavel Demin's `sdr_receiver_hpsdr` firmware, up
    to 8 receivers at 48 to 192 kHz each, or a Web-888 with its HPSDR image
    ([docs/WEB888_GETTING_STARTED.md](docs/WEB888_GETTING_STARTED.md)).
  - **FlexRadio** DAX-IQ through `flex_iq.py` (`skimmer_flex.example.json`).
  - Adding another radio takes one file that delivers IQ
    ([docs/RADIOS.md](docs/RADIOS.md#adding-an-iq-back-end)).

  Lost UDP packets are counted from the sequence number and their time kept as silence, so FT8
  slots stay on the clock (`FT8 trigger: ... lost=` in the log). A Red Pitaya streams to one
  client, and any client on the LAN can stop it: with no IQ for `hpsdr_restart_after_s` (default
  30 s; 0 = never) the receiver sends the start sequence again and logs `Receiver: no IQ ... sent
  the start sequence again`.
- **CW.** A scanner finds every signal in each band and opens one channel on it; each channel
  is decoded by **itila2**, a two-state HMM decoder with its timing fitted from the sender's own
  keying (`docs/ITILA2.md`).
- **Spotting the runner, not the caller.** The repeat spot rule (`spot_rule.py`) follows what
  RBN asks of a skimmer node: only the station calling CQ or TEST is spotted, after 2, 3 or 4
  copies by RBN's call pattern file (`patt3ch.lst`), with lock-on (who owns the frequency) and
  CQ qualifiers (`CQ PA`, `CQ FD`). Callers are never spotted on purpose.
- **RTTY.** RTTY signals are found in each band's RTTY segment and decoded once a minute, with
  the same repeat rule. `"rtty_decoder": "frtty"` decodes with FRTTY (`frtty.c`) in place of
  `rtty_core.c`: it models the whole character frame and the sender's cadence, over by over, so
  weak and fading signals copy better (JARTS 60 -> 65 real calls, PA 91 -> 97), at about 3x the
  RTTY decode CPU. For a contest weekend, `rtty_contest` adds that contest's rules:
  `"rtty_contest": "makrothen"` makes its words (MAK, MAKRO, MAKROTHEN, MKR) open a CQ group even
  when the CQ itself was lost, and reads a call followed by a grid square as a caller being sent
  the exchange (contests defined: `RTTY_CONTESTS` in `sparkgap.py`). `rtty_spot_cq_tail` also
  counts a call sent twice right before a closing CQ, TEST or QRZ after one other word (`JARL W2FM
  W2FM CQ`): a runner whose opening CQ was lost. `rtty_excludes_cw` keeps the CW decoder off a
  signal the RTTY decoder has read as RTTY.
- **FT8.** Decoded on each band's FT8 frequency with `decode_ft8` from Karlis Goba's
  [ft8_lib](https://github.com/kgoba/ft8_lib) (MIT) and uploaded to PSKReporter
  (`pskr_feeder.py`). Each cycle logs the median time offset (DT) of its decodes and warns when
  it says the PC clock is off.
- **SSB (voice), experimental.** Voices are found in the IQ by their filter shape and speech rhythm,
  transcribed with Whisper (faster-whisper `large-v3-turbo` on a CUDA GPU), and the station calling CQ
  is spotted on its own telnet port, by spoken versions of the same rules: runner copies, the better of
  two readings, lock-on. It runs on its own beside the CW skimmer: `voice/live_voice.py` on a growing
  IQ file, for example from `voice/flex_record.py` (FlexRadio DAX-IQ). How it works, how to run it and
  how it measured: `voice/README.md`.
- **Outputs.** DX-cluster spots on telnet port 7300 in CW Skimmer Server's format (RTTY lines as
  RTTY Skimmer Server writes them), so loggers, clusters and Aggregator can read them; an
  optional second port for your own use with its own, looser spot rule (see the quick start);
  `rbn2_feeder.py` uploads the CQ spots to RBN directly.
- **SparkGap Web**, in a browser: waterfall, call ladder and live decode
  text, and through each machine's SparkGap server (`sparkgap_server.py`) start, stop and
  settings for its copies (`docs/WEBUI.md`).
- **Tools.** An all-band IQ recorder (`iq_recorder.py`, `record_iq_dir`), a proxy that replays
  recordings as an HPSDR radio to any number of skimmers at once (`hpsdr_proxy.py`, with `--align`
  to keep FT8 slots on the clock and `--drop` to lose packets on purpose), file-mode replay, a band
  simulator with exact ground truth (`tools/sim/`), scoring against the RBN archive
  (`tools/eval/rbn_confirm.py`), refitting the spot validation weights from your own run logs
  (`tools/eval/fit_validation.py`), and a clip cutter to listen to a decode: frequency and time in,
  CW audio and the decoder's text out (`tools/listen/cw_clip.py`).

### Settings worth knowing

Off unless set, except where a default is given:

| Key | What it does |
|---|---|
| `spot_validation`, `spot_validation_calls` | Score each call before spotting it, using calls RBN skimmers heard (`rbn`) and the SKCC roster (`rbn+skcc`); `normal` is the tested level (`docs/ITILA2.md`) |
| `spot_rule_owned_two_windows` | A call heard on a frequency another call owns needs copies in two decode windows: a caller read in the runner's text waits |
| `rbn_known_drop_busts` | A call that reaches 10 RBN skimmers in a day only as a bust many of them share (a far busier look-alike that day) doesn't count as known; calls in MASTER.SCP always do |
| `itila2_tone_lock_hz` | Each bin locks on the tone that keys most of its time (the runner) and blanks a short over sent more than this many hertz from it (a caller off zero beat); 8 is the tested value |
| `spot_rule_window_join` | A CQ group cut by the edge of a decode window counts in the next window of the same bin |
| `spot_rule_split_up` | Split DX signing `TU <call> UP` is spotted |
| `spot_rule_cq_repeats`, `spot_rule_contest_words` | Back-to-back CQs count each; `CWT <call>` (and SST, MST) without a CQ opens a group |
| `spot_rule_fragments` | Pieces of a longer call heard near it are held back (K1V of K1VUT) |
| `signal_min_snr` | Scanner threshold to open a bin, dB over the local noise (12) |
| `itila_max_bins` | Most bins at once per band |
| `ft8_windows`, `ft8_band_cut` | `aligned` FT8 windows around each slot (keep the PC clock synced) or `sliding`; `fft` or `resample` band cut |

Work in progress is in the [open pull requests](https://github.com/hotairfred/SparkGap/pulls).

## Quick start (Linux)

Dependencies (Debian/Ubuntu):

```
apt install build-essential gcc python3 python3-numpy python3-scipy \
            libfftw3-dev pkg-config
```

PSKReporter and MQTT need `paho-mqtt>=2.0`, which Ubuntu doesn't package; use
`pip install -r requirements.txt` in a venv.

Build the C libraries (`make native ARCH=native` for `-march=native`):

```
make native
```

FT8 needs `decode_ft8` from [ft8_lib](https://github.com/kgoba/ft8_lib): `make ft8` fetches it
(pinned to the commit tested) and builds it beside `sparkgap.py`. To use one elsewhere, set
`"ft8_decoder": "/path/to/decode_ft8"`; or set `"enable_ft8": false`. SparkGap logs a warning
at start if FT8 is on and the decoder is missing.

Then set up your receiver, below, and run:

```
python3 sparkgap.py --config your_config.json
telnet localhost 7300
```

Every config needs at least your `callsign` and `grid`; the examples below show the rest.
`cw_decoder: itila2` (the default) is the current decoder with the repeat spot rule;
`cw_decoder: itila` selects the original chain ([docs/HISTORY.md](docs/HISTORY.md)).

**RX888 (or another radiod receiver).** Build and install ka9q-radio
([github.com/ka9q/ka9q-radio](https://github.com/ka9q/ka9q-radio), `docs/INSTALL.md`; `sudo make install`
puts in radiod, the RX888 firmware and its udev rules). Start from `radiod_sparkgap.example.conf`, one
192 kHz IQ channel per band at SparkGap's band centres (radiod names a channel's SSRC after its frequency
in kHz); its comments cover loopback multicast, the socket buffer, FFTW wisdom and checking the gain.
Then copy `skimmer_rtp.example.json`: `"sdr_type": "rtp"`, `rtp_group` (radiod's data group), `rtp_encoding`
(`f32le` or `s16be`, as radiod sends) and, on one computer, `rtp_iface` 127.0.0.1. CW, FT8, RTTY and IQ
recording all run from the channels. The RX888 has no input filtering, and everything on HF shares
its one ADC and gain: feed it from a protected receive path (a transceiver's receive loop, or a
limiter), a broadcast-band filter helps near strong AM stations, and keep it and its USB and coax
away from noisy computer power supplies, whose hash it hears on the low bands.

**Red Pitaya 125-14.** Install [Pavel Demin's `sdr_receiver_hpsdr`](https://pavel-demin.github.io/red-pitaya-notes/sdr-receiver-hpsdr/)
firmware and make sure it's reachable on your LAN. Copy `sk_5band.json` and edit at least:

```json
{
  "callsign": "YOUR_CALL",
  "grid": "YOUR_GRID",
  "sdr_ip": "192.168.X.X",
  "bands": [3590000, 7090000, 10118000, 14090000,
            18083000, 21090000, 24902000, 28090000],
  "cw_decoder": "itila2",
  "itila_decode_threads": 4,
  "enable_ft8": true,
  "enable_rtty": true,
  "rtty_spot_rule": "repeat"
}
```

`recent_band_floor.peers` ships empty. Any spot source you add there (another cluster on your
LAN, RBN's telnet) is connected at start even with `gate_recent_band_floor` off.

**Web-888.** [docs/WEB888_GETTING_STARTED.md](docs/WEB888_GETTING_STARTED.md): the HPSDR image,
six bands at 192 kHz, set up in SparkGap Web or from a config file.

**FlexRadio:** copy `skimmer_flex.example.json`; it uses a DAX-IQ channel, so CW Skimmer can
keep another channel on the same radio. If another receiver hangs on one of the radio's receive
loops, set `flex_loopa`/`flex_loopb` (and `flex_rxant`) to match: SparkGap's pan and slice come up
with the radio's band setting, and a loop switched off there is off for the whole antenna.
To run a radio's receivers from SparkGap Web, run `tools/sparkgap.sh setup` on the machine
beside the radio, then add receivers in SparkGap Web: the server runs each receiver, the radio's
SparkGap station and its RBN feeder, and gives each receiver its ports
([docs/SERVER_SETUP.md](docs/SERVER_SETUP.md), [docs/RADIOS.md](docs/RADIOS.md)). Without the
server, several copies on one radio (one band each, each on its own `flex_daxiq_channel`) share
one SparkGap station: start `python3 flex_station.py --config <one of their configs>` before them
and stop it after the last one. A copy started alone holds the station itself.
The decoders get silence while the radio is keyed, until 0.1 s after
its key-up (which comes after the transmitting client's CW delay), so
your own transmissions and their key clicks are not decoded.

**Your own spots.** Port 7300 (`telnet_port`) carries the RBN-grade spots, the same stream
`rbn2_feeder.py` uploads. If you want your own spots filtered differently, for example a looser
rule for your cluster or logger, add a second port with its own rule:

```json
"telnet_local_port": 7301,
"local_spot_rule": {"doubled_calls": true, "split_up": true, "fewer_repeats": 0}
```

`split_up` also spots DX working split from its sign-offs (`TU CY0S UP`), which CW Skimmer
does not do. Both port numbers are yours to pick (`--port` and `--local-port` override them).
Tweaks to `local_spot_rule` change only the local port; what goes to RBN stays the same.
Without `local_spot_rule`, 7301 carries the same spots as 7300. FT8 and RTTY go to both ports.
The local rule can also set its own `validation`, `validation_calls`, `cq_repeats` and
`contest_words`; without them it follows the top-level keys.

**macOS** should work with light Makefile changes (`.dylib` instead of `.so`; on Apple Silicon
omit `-march=native`). **Windows:** WSL2 with Ubuntu, then the Linux steps. Native builds for
both are welcome contributions.

## Feeding a cluster or RBN

Port 7300 speaks the same telnet format as CW Skimmer Server, so a DX cluster (we use
GoCluster), a logger or Aggregator can connect to it.

### PSKReporter (FT8) and MQTT

FT8 decodes travel from `sparkgap.py` to `pskr_feeder.py` through an MQTT broker (Mosquitto
or any other): SparkGap publishes one JSON message per decode on topic `skimmer/ft8/raw`, and
the feeder batches them to PSKReporter under your call and grid (`pskr_feeder.py --call
YOUR_CALL --grid YOUR_GRID`). The payload is described at the top of `pskr_feeder.py`
(`call`, `grid`, `freq_hz`, `snr`, `mode`, `msg`, `ts`). Both read the broker from the
environment: `MQTT_HOST` (default `localhost`), `MQTT_PORT` (1883), `MQTT_USER` and `MQTT_PASS`;
`deploy/sparkgap.service` and `deploy/pskr_feeder.service` show where to set them. SparkGap
connects at its first FT8 decode; if the broker can't be reached then, it logs `FT8 MQTT
publisher init failed (will not retry)` and runs on without FT8 uploads until it is restarted.
CW and RTTY spots don't use MQTT.

### RBN

`rbn2_feeder.py` uploads to RBN, set by the config's `rbn_upload` (`dry` to log what it would
send, `live` to send; anything else, or nothing, is off). Run `dry` first and check your node
against other skimmers before going `live`: the checks a node must make itself before a spot
reaches RBN (CQ only, repeats, mode codes, beacons, frequency) and what we measured are in
hotairfred/SparkGap#49. How to send spots: [docs/sending-spots.md](docs/sending-spots.md). The
feeders: [docs/rbn-feeding.md](docs/rbn-feeding.md). The RBN auth library the feeder needs:
[docs/rbn-auth-library.md](docs/rbn-auth-library.md).

## Sharing a receiver between computers

**RX888 / radiod.** `radiod`'s channels are multicast, so any number of programs can listen to the
same channels at once: a second SparkGap, a recorder, another skimmer. On one computer with
`ttl = 0` that needs nothing more; across the LAN, set `radiod`'s `ttl` with care, since every
channel then goes to every port of the switch that doesn't filter multicast (about 12 Mbit/s per
192 kHz float32 channel).

**Red Pitaya.** A Red Pitaya streams to one client. `hpsdr_proxy.py` takes that stream once and
passes it to any number of SparkGaps, so a second computer can take some of the bands:

```
python3 hpsdr_proxy.py --pitaya 192.168.1.54 --port 1025 --owner 192.168.1.76
```

- **The owner tunes the radio.** Only the client named in `--owner` sets the receivers' frequencies,
  the sample rate and the number of receivers; the others only listen. Give `IP:PORT` when the owner's
  computer runs more than one SparkGap: a bare IP makes every client on that computer an owner. Without
  `--owner`, every client's start sends its own frequencies, sample rate and receiver count to the radio,
  and the last one wins, so share a radio with `--owner`, or with every client on the same settings. A
  client's STOP never reaches the radio either way.
- **A listener lists the owner's bands** (the same receivers on the same frequencies, `max_receivers` the
  same) and decodes the ones it names with `rx_index`, for example
  `"bands": [{"center_khz": 14090, "rx_index": 3}, {"center_khz": 18083, "rx_index": 4}]`. A listener that
  asks for another frequency, or runs another sample rate or receiver count (it would read the IQ frames
  wrongly), is logged once (`! ... wants RX1 on 7000.0 kHz, the owner has it on 7090.0 kHz`), also when it
  started before the owner; its tuning never reaches the radio.
- **Point each SparkGap at the proxy** (`sdr_ip`, `sdr_port`). A SparkGap on the proxy's own computer
  needs its own `hpsdr_listen_port`: the receivers otherwise bind the port they send to, and take the
  proxy's packets.

Measured with a 2-receiver replay of a CWT (40 m and 20 m): the owner decoded both bands and a listener on
another computer decoded only 20 m through the proxy. All 29 of the owner's 20 m calls came out of the
listener too, at the same frequency (median 0.00 kHz). In a second run, a multi-band listener on the
proxy's own computer, with its own `hpsdr_listen_port`, spotted all 30 of the owner's calls. A receiver
tunes only the receivers it decodes, so a listener never retunes the owner's. Not yet run against a live
Red Pitaya.

## Replaying and measuring

File mode decodes a recording as fast as the CPU allows:

```
python3 sparkgap.py --config sk_5band.json --file recording.wav --center-khz 7090 \
                    --start-min 0 --end-min 15
```

It reads 16- and 24-bit stereo IQ WAV files at 96 or 192 kHz. Multi-band runs go through the
production C receiver path when the recording is replayed with `hpsdr_proxy.py` from another
host (`--negate-q` for skimmer1 captures). Public test recordings are in the repository's
releases (CY0S working split on 40m, a hand-sent 30m PA QSO Party segment, W1AW clips with
true text on the contributor fork).

Other tools: `rbn_tee.py` logs the worldwide RBN telnet feed; `SIGUSR1` to a running instance
dumps each band's IQ buffer to `/tmp`.

## Development

Contributions welcome: open an issue, then a small pull request with measurements. Tests run
with `pytest` (`tests/`) and in CI on Python 3.10 and 3.12. SparkGap is developed with Claude
Code as the main tool.

## License

GPLv3. See `LICENSE`.

## Authors

- Fred Krause, **WF8Z** (EM79SM), project owner. Contact info is on QRZ.
- Chris White, **WX7V**: the itila2 decoder and scanner, the repeat spot rule, the FlexRadio
  path and SparkGap Web.

## History

SparkGap grew out of luarvique's csdr-skimmer. From March to September 2026 it ran the original ITILA
decoder with a configurable set of spot gates; itila2 and the repeat spot rule became the default chain
in October 2026 (`cw_decoder: itila2`, #62), and `cw_decoder: itila` still selects the original one.
That chain, its gate flags and the comparisons made with it are in [docs/HISTORY.md](docs/HISTORY.md).

## Acknowledgments

This project stands on a lot of prior work — both software and theory.
Where source files are derived from another project, the original
copyright headers and license remain in those files; the list below is
the high-level acknowledgment.

The current decoder is **itila2** (Chris White, WX7V), built on the original **ITILA**
decoder (original to this project, named after MacKay's textbook). Several other decoders
ship in this tree as alternative paths and as research baselines —
they are not currently in the live spotting pipeline, but they are
included in the source distribution and their authors deserve credit
regardless.

### Software the live pipeline depends on

- **Pavel Demin** — Red Pitaya HPSDR firmware (`sdr_receiver_hpsdr`)
  and the `ft8d` standalone FT8 decoder.
- **Phil Karn (KA9Q)** — *ka9q-radio*: `radiod` turns a wideband receiver such as the RX888 into
  the IQ channels the `rtp` receiver reads.
- **Karlis Goba** — *ft8_lib* (MIT): `decode_ft8` decodes SparkGap's FT8.
- **OpenAI** — *Whisper*, and **SYSTRAN** — *faster-whisper* (both MIT): the transcriber behind
  the SSB voice skimmer.
- **Rudy Bakalov (N2WQ)** — *GoCluster*, BSD-licensed cluster server.
  Cross-source validation techniques (recent-on-band support floor,
  harmonic suppression) in this project's spot pipeline are adapted
  from GoCluster's design.

### Decoders shipped as alternative paths / research

These are present in the tree but not in the active pipeline. Each
file retains its original headers and license.

- **AG1LE (Mauri Niininen)** — *bmorse*, a Bayesian Morse decoder
  (`bmorse-skimmer.cpp`, `bmorse_lib.cxx`, `bmorse_src/`,
  `libbmorse.so`). Original copyright 2012–2014, GPL-3.0. Used during
  decoder research.
- **TF3LJ (Loftur E. Jonasson)** and the **UHSDR Firmware Project** —
  the in-process UHSDR CW decoder (`uhsdr_cw_lib.cpp`, `uhsdr_cw.cpp`,
  `libuhsdr_cw.so`). Original copyright 2016, GPL-3.0.
- **W1HKJ (Dave Freese)**, **AG1LE (Mauri Niininen)**, **OH2BNS
  (Tomi Manninen)**, **VE7IT (Lawrence Glaister)** — the *fldigi* CW
  decoder. `fldigi_cw.cpp` is a standalone port of fldigi's `cw.cxx`,
  GPL-3.0.
- **Marat Fayzullin (luarvique)** — *csdr-skimmer*, the project
  SparkGap was forked from (GPL-3.0). Its original CW and RTTY skimmer
  programs are still here: `cw-skimmer.cpp`, `rtty-skimmer.cpp` and
  `bufmodule.cpp`, built by `make csdr` and packaged as
  `csdr-cwskimmer` and `csdr-rttyskimmer`.
- **HA7ILM (Andras Retzler)** and **Marat Fayzullin (luarvique)** —
  *libcsdr* / *csdr-cwskimmer*. `libcsdr_cw.cpp` derives from this
  work, BSD-licensed.
- **Erik de Castro Lopo** — *sndfile-spectrogram*, code referenced by
  bmorse.

### Theory and framework

- **David MacKay** — *Information Theory, Inference, and Learning
  Algorithms* (Cambridge University Press, 2003). The Bayesian
  framework behind the ITILA decoder comes from there; the decoder
  itself is original to this project.

### Community

- Everyone who's run a CW skimmer for the last 15 years and kept the
  RBN ecosystem alive.
