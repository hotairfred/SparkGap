# Web-888: getting started

How to prepare a new Web-888 and run it with SparkGap as an RBN node, six
bands at 192 kHz. The design behind it is in
[WEB888_DESIGN.md](WEB888_DESIGN.md).

Part 4 sets it up in SparkGap Web, the usual way. Part 3 runs it from a
config file you write, to check the unit by hand first.

## 1. What you need

- The Web-888 and its USB-C cable. Resellers list no SD card, power supply
  or GPS antenna in the box; check yours.
- A **5 V, 2 A or better USB-C supply** and a short, heavy cable. The unit
  draws over 1 A, peaks near 1.5 A, and may not boot below about 4.75 V. A
  linear supply and a ferrite on the cable lower LF and MF noise.
- **Two microSD cards, SD or SDHC, 32 GB or smaller (not SDXC)**, Class 10:
  one for the stock system, one for HPSDR. Label them.
- A card reader on any computer.
- A **gigabit** Ethernet port and a shielded Cat 5e or better cable. Six
  receivers at 192 kHz send about 55 Mbit/s.
- An **active** GPS antenna with a view of the sky (the unit feeds it 3.3 V).
  Optional for SparkGap, which takes its time from the host.
- The HF antenna, with **protection for the input**: a strong signal (your own
  transmitter, a nearby station) can destroy the ADC, and the vendor's
  warranty does not cover it. A broadcast-band filter helps where shortwave
  broadcasters overload the receiver.
- The SparkGap machine on the same LAN.

## 2. Prepare the Web-888

### Unpack

Check the fan turns freely, with no foam or cable against its blades. For
running all day, give it air and keep the fan free of dust.

### The stock card (first check)

The stock system is the easiest way to see that the radio, antenna and
network work before switching to HPSDR.

1. Format the first card FAT32.
2. Download the latest stock image, `web-888-alpine-3.20-armv7-<date>.zip`,
   from [RaspSDR/server releases](https://github.com/RaspSDR/server/releases).
3. **Unzip it to the card's top folder.** Do not write it with an image tool
   (Etcher, dd) and do not leave it inside an extra folder: either gives a card
   that does not boot.
4. Insert the card **label side down**, connect antenna, GPS antenna and
   Ethernet, then power.
5. Watch the LEDs: **D0 blue** means Linux is up; **D2 green flashing** means
   the web server runs. D2 green staying on means the card did not boot:
   format and copy it again. The first boot is slow.
6. Open `http://web-888.local:8073` (or the address your router gave it).
7. In `http://web-888.local:8073/admin`: set the admin password, set updates
   to Stable and update, and watch the GPS tab until it has a fix (several
   minutes the first time). Leave "External ADC clock" at No unless a 10 MHz
   reference is connected (with it on and none connected, the waterfall stays
   black).
8. Tune a few CW signals you know. Look for overload from broadcast stations
   and try the attenuator; note what works. This is your baseline.

In your router, **reserve the Web-888's address** (by its MAC) so it keeps one
address in both systems.

### The HPSDR card

1. Format the second card FAT32.
2. Download `red-pitaya-alpine-3.20-armv7-20241228.zip` (v0.0.2) from
   [RaspSDR/red-pitaya-notes releases](https://github.com/RaspSDR/red-pitaya-notes/releases).
   Use this build, not Pavel Demin's Red Pitaya image: the Web-888 port sets
   its 122.88 MHz clock and its GPIO.
3. Unzip it to the card's top folder, as above.
4. So the HPSDR receiver starts at every boot, copy
   `apps/sdr_receiver_hpsdr/start.sh` from the card to the card's top folder.
5. Power off, swap cards, power on.
6. The Web-888 takes an address by DHCP (falling back to 192.168.1.100).
   Use its address, from your router or discovery (below): `web-888.local`
   does not resolve on this system. `http://<address>/` (port 80) lists the
   apps; "SDR Receiver compatible with HPSDR" is the one SparkGap uses. SSH is
   `ssh root@<address>` (Pavel's default password is `changeme`; change it if
   it works).

To go back to the stock system, power off and swap the cards. Only one runs at
a time.

## 3. Run SparkGap on it

### Find it

On the SparkGap machine:

```text
cd ~/sparkgap/code
~/sparkgap/.venv/bin/python hpsdr_receiver.py --discover
```

It should list the Web-888's address and MAC, board ID 6 (Hermes Lite),
firmware 73 and 6 receivers. Nothing listed: check the LEDs, the address in
your router, and that the machine is on the same subnet (discovery is a
broadcast).

### Check it with another client (optional)

Before SparkGap, a known client proves the HPSDR side: SDR Console ("Hermes
Lite" receiver) or CW Skimmer Server with
[HermesIntf](https://github.com/k3it/HermesIntf) (six bands at 192 kHz). Close
it afterwards: the Web-888 sends its stream to whichever client started last,
so a second client takes it from SparkGap.

### The config

Write `~/sparkgap/config/Web888.local.json`, with your own spotter ID and grid
(the Web-888 is its own RBN node, so it needs its own spotter ID) and the
address from discovery:

```json
{
  "callsign": "<your call>",
  "skimmer_suffix": "-<n>",
  "grid": "<grid>",
  "sdr_type": "hpsdr",
  "sdr_ip": "<Web-888 address>",
  "sdr_port": 1024,
  "sample_rate": 192000,
  "max_receivers": 6,
  "bands": [3590000, 7090000, 10118000, 14090000, 18083000, 21090000],
  "lna_gain": 20,
  "ppm_offset": 3.9,
  "telnet_port": 7330
}
```

<!-- markdownlint-disable MD013 -->
| Key | Why |
| --- | --- |
| `bands` | Six centres, one per receiver: 80, 40, 30, 20, 17 and 15 m here. Each covers 192 kHz around its centre. |
| `max_receivers` | 6: the Web-888 has six. |
| `lna_gain` | Leave at 20: on the Web-888 this word sets the attenuator, and 20 leaves it alone. 40 would add 8 dB of attenuation. |
| `ppm_offset` | SparkGap tunes 3.9 ppm low, a correction for the Red Pitaya. 3.9 cancels it on the Web-888; then set it from RBN's frequency skew (below). |
| `telnet_port` | Where the spots come out, for the feeder. |
<!-- markdownlint-enable MD013 -->

Add the decoder and spot options your other receivers run (the set under
test is in cdub89/SparkGap#46).

### Run it

```text
cd ~/sparkgap/code
~/sparkgap/.venv/bin/python sparkgap.py --config ~/sparkgap/config/Web888.local.json
```

Within a minute the log shows the six receivers live, packets arriving with no
losses, and the first `ITILA raw` lines. A single band starts the same way
with one entry in `bands`; start with one if anything looks wrong.

To send spots, run the feeder against its telnet port as for any receiver
([sending-spots.md](sending-spots.md)), dry first.

### Frequency correction

The Web-888's clock is a 0.5 ppm TCXO, and HPSDR mode does not correct it from
GPS. After a day of spots on RBN, read the node's frequency skew in ppm from
RBN's skimmer accuracy figures (or compare its spots with nearby skimmers'
for the same calls). Add it to `ppm_offset` (spots running high are a
positive skew) and restart. Let the unit run 24 hours after
any change before correcting again: it drifts while it warms up.

## 4. With SparkGap Web

In SparkGap Web the Web-888 runs like the Flex radios:

1. In SparkGap Web, open the server and, under **Add a radio**, choose
   **Type** HPSDR, give it a **Name** if you like (empty: `Web888`) and its
   address (empty: search the network), then **Find**. It writes
   `config/<name>.radio.json` and becomes the radio Setup shows: set its
   spotter ID, grid and RBN upload there. Setup always shows the radio picked
   in the header; **Rename** (while it is stopped) and **Remove** are there
   too. `sparkgap.sh setup` asks the same.
   From the command line: `sparkgap_ctl.py discover hpsdr <address>`.
2. **+ Add a receiver** under `Web888` takes a band for the next receiver
   (`Web888-1`, then `-2`, up to the radio's six). The first one creates the
   radio's SparkGap from `skimmer_hpsdr.example.json`, with its own telnet port
   and `hpsdr_listen_port`, so a second HPSDR radio can share the computer.
   **Remove** on a receiver's card takes its band out; the receivers after it
   move up a number (`Web888-4` becomes `-3`). The last one, once stopped,
   takes the radio's SparkGap out, its config kept. Setup's **Remove** takes
   out a radio with no receivers left, keeping its file as
   `<name>.radio.json.removed-<time>`.
3. Each receiver has its own card (`Web888-1` to `Web888-6`) with its band and
   spots. Starting any receiver starts the radio; a band change on one card
   restarts all six receivers for about 10 seconds. Decoder options are shared
   by the six. `sparkgap.sh status` lists the same receivers;
   band changes and everything else besides setup, start and stop are in
   SparkGap Web, whose address ends each `sparkgap.sh` output.
4. The radio bar shows Connected while packets arrive, and how many of the six
   receivers are in use.
5. With the Web-888 beside a FlexRadio, each is its own node. The radio bar
   becomes a switch: pick `Web888` or the Flex to see its node, feeder and
   receivers.

### A band change, round trip

Six receivers and nine bands: a fixed layout keeps band changes rare. Here
`Web888-1` to `Web888-5` hold 80, 40, 20, 15 and 10 m, and `Web888-6` swings
through 60, 30, 17 and 12 m. At dusk 10 m closes, so `Web888-5` goes to 30 m;
at dawn it comes back.

1. Pick `Web888-5` in SparkGap Web and choose 30 m. SparkGap writes the band
   into `Web888.local.json`.
2. The radio's process restarts: all six receivers go quiet for about 10
   seconds, not only `Web888-5`. The feeder loses port 7330 and reconnects.
3. The other five bands start over too: calls already spotted there are
   spotted again in the next minutes, and what the spot rules know about each
   call builds up from zero.
4. At dawn, choose 10 m: the same restart again.

By hand, the same change is an edit of the receiver's entry in `bands` and a
restart of `sparkgap.py`. 60 m has no band name: give it as a centre,
`5368000`, which covers the US channels. A later stage moves one receiver
without the restart ([design stage 7e](WEB888_DESIGN.md#changes-by-stage)).

## When something is wrong

<!-- markdownlint-disable MD013 -->
| What you see | Likely cause |
| --- | --- |
| D2 green stays on after power-up | The card did not boot: SDXC card, image tool, or an extra folder level |
| Freezes or drops out | Power: supply under 2 A, long or thin cable |
| Discovery lists nothing | Not on the same subnet, or the HPSDR app is not running (check `start.sh` on the card) |
| Stream stops when another program starts | Another HPSDR client took it: only one at a time |
| Spots a few tens of Hz off | `ppm_offset` not yet set from RBN's skew |
| Noise everywhere at night, many phantom signals | Broadcast overload: attenuate, or filter the input |
| Lost packets in the log | A 100 Mbit link or a busy switch: use gigabit |
<!-- markdownlint-enable MD013 -->

## Sources

[Web-888 overview](https://www.rx-888.com/web/),
[requirements](https://www.rx-888.com/web/guide/requirements.html),
[power](https://www.rx-888.com/web/guide/power.html),
[troubleshooting](https://www.rx-888.com/web/guide/breakdown.html),
[HPSDR app](https://www.rx-888.com/web/redpitaya/sdr-receiver-hpsdr.html),
[Pavel Demin's Alpine notes](https://pavel-demin.github.io/red-pitaya-notes/alpine/),
[hamparts: Web-888 as a skimmer server](https://hamparts.shop/blog/web-888-as-cw-rtty-and-digi-skimmer-server.html).
