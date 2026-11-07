# Web-888 back end: design

How SparkGap runs a Web-888 as an RBN node: the radio, its receivers and its
feeder, the same model as FlexRadio ([RADIOS.md](RADIOS.md)), on the HPSDR
back end SparkGap already has. For cdub89/SparkGap#5 and stage 7 of
[RADIOS.md](RADIOS.md#stages). Operator steps are in
[WEB888_GETTING_STARTED.md](WEB888_GETTING_STARTED.md).

Status: built and live-tested on our Web-888 (cdub89/SparkGap#47): stages 7a to
7d; 7e (one receiver's band change without a restart) is deferred. Facts still
marked *(to check)* are from the firmware's source or from users.

## The device

The Web-888 (SDDC Lab, [www.rx-888.com/web](https://www.rx-888.com/web/)) is
a 16-bit HF receiver on a Zynq 7010: LTC2208 ADC clocked at 122.88 MHz from a
0.5 ppm TCXO, 0 to 61.44 MHz behind a 64 MHz low-pass filter and a +20 dB
LNA, a 0 to 31.5 dB attenuator, GPS, gigabit Ethernet, 5 V over USB-C. It
boots whatever system is on its microSD card. Three systems matter:

<!-- markdownlint-disable MD013 -->
| System | What it serves | For SparkGap |
| --- | --- | --- |
| Stock (KiwiSDR-style web SDR, [RaspSDR/server](https://github.com/RaspSDR/server/releases)) | 13 channels of 12 to 36 kHz, web UI on port 8073 | No: too narrow for whole-band IQ |
| Red Pitaya Notes port, `sdr_receiver_hpsdr` ([RaspSDR/red-pitaya-notes](https://github.com/RaspSDR/red-pitaya-notes/releases) v0.0.2) | HPSDR Protocol 1: **6 receivers**, one rate for all of 48, 96, 192 or 384 kHz | **Yes**: the path in this design |
| Same image, `sdr_receiver` (Pavel Demin's SMEM protocol, TCP 1001) | 8 receivers at up to 192 kHz | Later, a separate back end |
<!-- markdownlint-enable MD013 -->

The vendor page says "eight receivers" for the HPSDR app; that text is copied
from Pavel Demin's Red Pitaya page. The Web-888 build has 6 DDCs and reports 6
(`NUM_CHANNELS 6`; RaspSDR/red-pitaya-notes#3 "384Khz 6channels"). Six bands
at 192 kHz is also what CW Skimmer Server gets from it with HermesIntf.

### HPSDR mode, as the firmware implements it

- Discovery on UDP 1024 (seen on our unit): a 63-byte reply with a MAC that
  is fixed text, not the unit's (`64:69:73:74:72:6f`, "distro" in ASCII, so
  likely the same on every unit of this image), firmware version 73,
  **board ID 0x06 (Hermes Lite)** and the receiver count (6) in byte 19; byte
  2 reads 3 while another client holds the stream. Pavel's Red Pitaya build
  answers board 0x01, version 25, 8 receivers.
- Data: standard EP6 packets, 24-bit I/Q per receiver.
- Control: rate in C1 bits 0-1, receiver count in C4 bits 3-5, one NCO per
  receiver (1 kHz to 61.44 MHz, about 0.11 Hz steps).
- Attenuator at C0 0x0A: C4 bit 6 set means the low 6 bits are attenuation in
  0.5 dB steps; otherwise, with bit 5 set, `(C4 & 0x1f) * 2` steps. This is
  attenuation where Hermes-Lite 2 clients mean gain *(to check)*.
- Dither, PGA and random bits are accepted but not wired in the bitstream;
  no ADC overload bit in EP6 *(to check)*.
- **No clock correction**: GPS sets system time only. The client corrects
  frequency.
- **One client at a time**, and the last one to send start takes the stream:
  no lock, no watchdog.

## What SparkGap had

The HPSDR receiver is Fred's production path (a Red Pitaya, 8 bands at
192 kHz, `sk_5band.json`):

- `hpsdr_fast.c` (`libhpsdr_fast.so`): one UDP stream, up to 8 receivers,
  lost-packet counting and gap fill, restart after silence, a decode worker
  per receiver. Used when a config has more than one band.
- `hpsdr_receiver.py`: discovery (`--discover`), and the receiver used for a
  single band. It sits at its packet-rate limit at 192 kHz.
- `hpsdr_proxy.py`: shares one radio between processes (SkimSrv beside
  SparkGap).
- One `sparkgap.py` process runs every band of the radio; each `bands[]`
  entry is one receiver; one telnet port carries all of them.

Jouni (j0uni) ran this code on a Web-888 unchanged in September
(`web888-macos-cw-monitor`, 93d08f4: 6 receivers, 192 kHz, UDP 1024), so the
protocol works. What was missing is the control plane: `radios.py` had only
the Flex back end, so a hand-written HPSDR config showed under "receivers with
no radio file", with no Setup, Add or Remove.

Left as they are, for later changes:

- **Frequency correction is Red Pitaya's.** `hpsdr_fast.c` and
  `hpsdr_receiver.py` tune every receiver 3.9 ppm low, the STEMlab's
  correction. On a Web-888 `ppm_offset` 3.9 cancels it (Add writes it for a
  Web-888); the unit's own TCXO offset is then set from RBN's skew.
- **`lna_gain` is Hermes gain; the Web-888 reads attenuation.** The shipped 20
  sets neither bit 5 nor bit 6, so the attenuator is left alone.
- **Health is thin.** The C receiver reports `lost` as ring overflows
  (`drop_count`), not lost packets (`pkt_lost`), and never shows `restarts`.
- **Results go to the first band.** Every decoded window is filed under the
  first band's scanner; bins are keyed by frequency, so nothing collides.
- **One band uses the Python receiver**, at its limit at 192 kHz: give an
  HPSDR radio two bands or more.

## The model

Same words as Flex, so SparkGap Web, the command line and the logs read the
same on every SDR:

- **Radio** (`Web888`): the device and the RBN node (one spotter ID, one
  feeder). The name is the operator's, given in Setup.
- **Receivers** `Web888-1` to `Web888-6`: one band each, numbered by their
  place in `bands[]`.
- **Feeder**: one per radio, as on Flex.
- **No station.** HPSDR has no shared client to hold; the skimmer process holds
  the stream itself.

What differs from Flex, because the protocol has one stream per radio:

- **One skimmer process per radio**, named after it and holding all its
  receivers (Flex: one per receiver). Two processes cannot share the stream:
  the last start wins.
- **Start and stop act on the radio.** Starting `Web888-3` starts the radio's
  process; removing its last receiver takes the process out.
- **One telnet port and one `hpsdr_listen_port` per radio**, so two HPSDR
  radios can share a computer.
- **A band change restarts the process** (all receivers, about 10 s); stage
  7e, one receiver's band change without a restart, is deferred.
- **Every receiver has the same width** (the radio's rate).

## Files

`config/Web888.radio.json`, written by Add a radio's Find and by Setup:

```json
{
  "type": "hpsdr",
  "nickname": "Web888",
  "model": "Web-888",
  "address": "192.0.2.60",
  "board_id": 6,
  "firmware": 73,
  "receivers": 6,
  "callsign": "<spotter ID>",
  "grid": "<grid>",
  "op_name": "<name>",
  "rbn_upload": "dry"
}
```

`config/Web888.local.json`, the radio's one skimmer config, written by its
first Add from `skimmer_hpsdr.example.json`: the decoder and spot options,
`bands[]` (one per receiver, in order), its own `telnet_port` and
`hpsdr_listen_port`, `max_receivers` from discovery, and `ppm_offset` 3.9 for
a Web-888. Its address, spotter ID and grid come from the radio file.

Model from discovery: board 0x06 with firmware 73 is a Web-888 (default name
`Web888`); anything else is "HPSDR board N" (default `HPSDR`). A radio is
known by its address, reserved in the router: the discovery MAC is fixed text
and cannot tell two Web-888s apart. A second unit at another address gets
`<name>_<last octet>` (`Web888_121`) unless the operator names it.

## What was built, by stage

**7a. Run by hand** (no code change): a hand-written config, one band then six
at 192 kHz, no lost packets. The 2026-11-06 SST on 20m: 143 spots, 62 calls,
97.9% of spots confirmed by other skimmers.

**7b. HPSDR back end** (`radios.py`, `hpsdr_receiver.py`): discovery by
broadcast or at an address; board ID, firmware and receiver count from the
reply; the model named; the busy byte reported ("in use by another client").
Discovery takes a radio type; without one it finds a FlexRadio, as
`sparkgap.sh setup` used to. An HPSDR radio found can be given the operator's
name: a file name, unique, and never `<x>-<n>`. Capabilities: no live band
change, no station.

**7c. The server runs it** (`seat.py`, `sparkgap_server.py`, `sparkgap.py`):

- A receiver shows under its radio file whatever its back end; `radio()` still
  means the Flex station a receiver needs, so none starts for HPSDR.
- An HPSDR receiver of a radio file takes `sdr_ip` from that file.
- Each receiver's band, spots, calls and windows from the log (its
  `SparkGap LIVE` and spot lines), running or stopped; its open bins from the
  skimmer link, sent every 2 s.
- A band change for one receiver (`rx_bands`) rewrites that entry and restarts
  the radio's process.
- Add: the next receiver and a band; the first creates the process from
  `skimmer_hpsdr.example.json` with a free telnet port and `hpsdr_listen_port`
  (1024, then the next free). Remove: the band leaves `bands[]`, the later
  receivers move up a number, and the radio restarts if it runs; the last,
  stopped, takes the process out, its config kept.
- Rename an HPSDR radio while stopped: radio file, receivers' `radio`, its
  instances line and config. Remove a radio once no receiver names it: its
  file kept as `<name>.radio.json.removed-<time>`.
- A command naming `<radio>-<n>` acts on the radio's process; the busy check
  is the radio's, so two changes to one radio never overlap.
- Band limits: an unset `skim_bandwidth_khz` is the receiver's span, as SETT
  and the RBN band list report it (hotairfred/SparkGap#133).

**7d. SparkGap Web and the command line**: with more than one radio, the
header is a radio switch (name, model, spotter ID), remembered in the browser.
Setup shows the radio picked there (type, spotter ID, grid, operator, RBN
upload, name, removal), and Add a radio asks type, name and address with one
Find. An HPSDR radio has a card per receiver (open bins, spots) and its
process on a Skimmer line; a receiver's pane shows its own spots, calls,
windows and bins with the radio's totals. `sparkgap.sh status` shows the same
(a `skimmer` row, a row per receiver), and `sparkgap.sh setup` asks the radio
type every time and an HPSDR radio's name. The spectrum tap computes and sends
only the band a browser's waterfall shows.

**7e. One receiver's band change without a restart**: deferred until needed.
`hpsdr_set_freq` already updates the receiver's NCO in memory; it would send
the frequency at once and replace only that band's scanner.

**Later, separately:** frequency calibration on the radio file and the
Web-888's attenuator in steps; lost packets and restarts in the health line;
each window filed under its own band; the C receiver for one band; stable
receiver numbers (`rx_index` with band names in `_resolve_band`); the
8-receiver SMEM back end (`sdr_receiver`, TCP 1001) if six bands are not
enough; 384 kHz receivers; HPSDR on stock firmware (j0uni/web888-ng, 48 kHz
only).

## Decisions

1. **HPSDR Protocol 1 on the RaspSDR image** as the Web-888 path, six bands at
   192 kHz; SMEM (8 bands) only if six prove too few.
2. **One process per radio** with receivers inside it, against the Flex
   pattern of one per receiver. The protocol leaves no choice; the cost is that
   one band's change or crash restarts all six.
3. **Frequency correction by `ppm_offset`**, set from RBN's skew, not GPS: the
   HPSDR image does not discipline the clock.
4. **No sharing.** SkimSrv or another client starting a stream takes the
   Web-888 from SparkGap. `hpsdr_proxy.py` can share it later if wanted.

## Still to check on a unit

- Attenuator direction and steps; whether dither or PGA do anything.
- Overload on a strong broadcast band (no overload bit; no auto attenuator).
- Frequency offset over a day, cold and warm (wait 24 h after any change
  before correcting).
- Root password on the HPSDR image (`changeme` on Pavel's).

## Sources

- Vendor: [overview](https://www.rx-888.com/web/),
  [hardware](https://www.rx-888.com/web/hardware.html),
  [Red Pitaya port](https://www.rx-888.com/web/redpitaya/),
  [HPSDR app](https://www.rx-888.com/web/redpitaya/sdr-receiver-hpsdr.html),
  [requirements](https://www.rx-888.com/web/guide/requirements.html).
- Firmware: [RaspSDR/red-pitaya-notes](https://github.com/RaspSDR/red-pitaya-notes)
  (`projects/sdr_receiver_hpsdr/server/sdr-receiver-hpsdr.c`, v0.0.2),
  [Pavel Demin's notes](https://pavel-demin.github.io/red-pitaya-notes/sdr-receiver-hpsdr/).
- CW Skimmer Server: [k3it/HermesIntf](https://github.com/k3it/HermesIntf).
- Prior SparkGap run: `j0uni/SparkGap@web888-macos-cw-monitor` (93d08f4).
