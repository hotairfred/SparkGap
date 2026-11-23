# SparkGap Web (experimental)

SparkGap Web shows your SparkGap receivers in a web browser. You see the band as
a waterfall, every call SparkGap hears marked by frequency, and the CW it copies
from the station you pick. From the same page you can add, start and stop a
receiver, move it to another band, and change how it spots. Nothing to install:
any browser on your network will do, and closing the page does not affect the
receivers.

![SparkGap Web on 15m](web.png)

## Getting connected

You need two things from whoever set up SparkGap on the radio's computer: the
page's address (for example `http://192.168.1.20:7320/`) and a six-character
join code (for example `KTR-49X`).

1. Open the address in your browser.
2. Click **⚙**, then **+ Add a server**, enter the address and the join
   code, and save.
3. The waterfall starts. If the computer runs more than one receiver, pick
   one from the band list at the top.

The page remembers the server, so next time it opens straight to the
waterfall. A wrong code five times in a row locks your browser out for ten
minutes.

## The main screen

- **Waterfall**: the whole band the receiver listens to. Signals are the bright
  traces.
- **Call ladder**, beside the waterfall: every signal by frequency. Spotted
  calls stand out, calls heard but not spotted are dimmer, and signals with no
  call yet show only their frequency.
- **Decoded text**, on the right: click a trace or a call to see what SparkGap
  copied from that station, newest first, with its frequency, speed and signal
  strength. You can select the text, or **Copy** all of it. With both readings
  shown, the one SparkGap is surer of (the lower timing cost) is in bold; a
  reading that copied nothing shows as *(nothing decoded)*.
  Where two stations sit close together, such as the two sides of a QSO, a
  click on the trace picks the one sending now; the other has its own row in
  the call ladder.
- **Switching receivers**: each receiver comes back on the signal you last
  picked on it. Two receivers that hear the same frequency (two antennas or
  radios on one band) keep the same signal, so you can compare their copy
  back and forth.
- **Search** (Ctrl+F): type a call, some text or a frequency in kHz to jump to
  it. With the ⚙ dialog open, Ctrl+F is the browser's own find instead.
- **Zoom**: drag to move along the band; Ctrl+scroll or pinch to zoom. The
  **Zoom** button steps through the whole band, half and a quarter. Each
  receiver opens at the zoom you last left it at, in this browser.
- **Bottom bar**: the connection (a green dot and Live when all is well;
  **Reconnecting to radio** or **Radio not connected** when the receiver is
  running but its radio is not), how hard the computer is working, how many
  calls the receiver has spotted, and how long it has been running. On the
  right, the server log's newest line; click it to open the log.
- **?** (inside the ⚙ dialog): this guide and what's new.

## Radios and receivers

Click **⚙**. The top line shows the radio: **Connected**, **Connecting**,
**Reconnecting** or **Not connected**, its model, address and pans in use.
The left side lists each server's radios. Under each radio:

- **Node**: the spotter ID, grid and operator its spots go out under,
  whether RBN upload is off, dry or live, and what every receiver shares:
  recording and decoder options (a Web-888 also its run switch and details).
- **Feeder**: what sends those spots to RBN, and whether it is signed in.
- **Receivers**: each one with its band and state. Click one to see its
  settings on the right.
- **+ Add a receiver**: pick a free DAX-IQ channel and a band, check the
  decoder options, and Apply. The receiver starts at once.

Below the radios, **Server** holds the join code, Setup, RBN upload, the
server's log, and **Downloads**: each file SparkGap fetched (the RBN auth
library, the SKCC roster, the RBN known calls, RBN's `patt3ch.lst`,
beacon list and `master.scp`), its record count, when it downloaded, where it
is, and the last failed fetch, as `sparkgap.sh status` lists them. A failed or
empty fetch keeps the previous copy. **Check for updates** compares each file's
date on its server with our download; **Refresh** fetches them all now.
Receivers read a new copy at their next start.

![The Server pane](web-server.png)

A receiver's settings:

- To change a setting, pick the new value. A bar at the top lists the changes
  not applied yet, with **Apply** (it says whether the change is live or
  restarts, starts or stops the receiver) and **Discard**. You can change
  several at once; they take effect together.
- **RUN**: on starts the receiver, off stops it.
- **BAND**: changes straight away while the receiver runs, in about a
  second, and the waterfall starts over on the new band. A receiver set up to
  listen at an exact frequency rather than a band shows that frequency first
  in the list, for example `7038.0 kHz`; picking a band name moves it to that
  band.
- **ANTENNA** and **RF GAIN** (FlexRadio, while the receiver runs): as the
  radio has them right now, however they were set: here, in SmartSDR or
  Maestro, or by another receiver. They follow the radio within a couple of
  seconds, and a change here goes to the radio straight away. The receiver
  starts with the antenna and RF gain set in its config, or the radio's where
  none is set.
- Antenna, loops and RF gain are shared: on a FlexRadio, every panadapter on
  the same receiver (SCU) has the same antenna and RF gain, and the radio
  decides which SCU a panadapter uses. Changing one receiver can change
  another, or an operator's panadapter in SmartSDR or Maestro.
- **RECORDING**: on the Node, for every running receiver (a receiver with no
  radio file has its own). It starts and stops without a restart, and is off
  after a restart. A Web-888 records every band: it
  starts at once, its files appear at the next minute, and it stops at the
  next minute; while its RECORD file is there, it records across a restart.
  While it records, the Node card says so and the switch shows on in orange.
- **DECODED TEXT**: SparkGap reads each signal twice, through a wide and a
  narrow filter. **Lowest cost** (the default) shows whichever it is surer of;
  you can show both, or only one. This is for this browser, all receivers.
- **Decoder options** (click to open): the spot rule, call validation, the
  known call list, the decode window, AFC and, under the Gated Rule, its
  filters. The line beside it sums them up. Changes restart the receiver.
  **Reset to defaults** goes back to the shipped settings.
- **Window (Experimental)**: how much signal each decoded window holds.
  Shorter windows show text sooner but give the decoder less signal to work
  from and use more CPU; 30 and 60 s are measured, 20 and 10 s untried.
- **Details** (click to open): health, packets, reconnects, recent warnings
  and errors.
- **Remove**: stops the receiver and takes it off the radio's list, on
  Apply. Its config, logs and recordings stay; adding the same receiver again
  brings its settings back.

![A receiver's settings](web-settings.png)

Settings, in plain terms:

- **Recording**: saves the raw signal to disk so it can be replayed later. It
  uses a lot of space (it needs at least 5 GB free); leave it off unless you
  were asked to record.
- **Spot Rule**: the Repeat Rule spots a call after enough copies of it in
  CQs. The Gated Rule spots a call only when it passes each of 12 yes/no
  checks in order, and the log names the check that decided
  ([SPOT_RULES.md](SPOT_RULES.md)).
- **Call validation**: how sure SparkGap must be before it spots a call.
  Permissive spots more calls but gets more wrong; aggressive spots fewer but
  surer ones. Normal suits most stations. Off uses no score: a call is spotted
  once it is copied a set number of times (2, 3 or 4, by how common its
  pattern is), and the known call list is not used. Under the Gated Rule it
  steps the reads a call needs instead: permissive one fewer, aggressive one
  more; unset is normal, so only those three show.
- **AFC**: each signal's decoder follows the signal's own tone, which helps
  weak signals and ones slightly off tune. Changing it restarts the receiver.
  It acts only with the itila2 decoder and is greyed out with any other.
- **Known call list**: calls on this list count as more likely to be right.
  SCP is the contest call list; RBN adds calls other RBN skimmers heard in the
  past week; RBN + SKCC adds the SKCC roster, good for hand-keyed CW. Under
  the Gated Rule a call not on the list needs one more read. None: no list.
- **Gated Rule filters**, shown only under the Gated Rule, as the Aggregator
  has them: **Bad Calls** (calls in `BadCalls.txt`), **Notched Freqs**
  (frequencies in `NotchedFreqs.txt`), each with the file's path beside the
  config, and **MASTER.SCP Filter** (no spot for a call that is in neither
  MASTER.SCP nor the known call list, beacons excepted). All are off unless set.

The Repeat Rule's own options (contest words, CQ repeats, fragments and the
rest), Strongest copy and Decoder SNR are config keys, not shown here: the
shipped defaults suit most stations. A config with `local_spot_rule` keeps its
own validation and spot rule for the local port, which these settings do not
change.

Hover over a setting for a one-line reminder.

## When something looks wrong

- **The Radio line** at the top of ⚙ says whether SparkGap holds the radio:
  - **Connecting**: starting; it turns Connected within a few seconds.
  - **Reconnecting**: SparkGap lost the radio (rebooted, off the network)
    and reconnects by itself once the radio has answered steadily for a
    minute. If it stays this way, check the radio and its network cable.
  - **Not connected**: no receiver runs on the radio, or SparkGap gave up
    after 10 minutes. Fix the radio, then turn the receivers' **RUN** on.
- **The server's log** (⚙, **Server**, Log): every start, stop and setting
  change with its result, radio connection changes, and every warning and
  error from the receivers, the station and the feeder. It is the first thing
  to look at, and the thing to send along when asking for help.
- **A receiver's Details** start with a line: green means the radio is live
  and decoding keeps up. Otherwise it says what is wrong, worst first:
  - **Radio reconnected**: SparkGap lost the radio for a moment, usually a
    network hiccup, and reconnected by itself.
  - **Decoder behind**: the computer is too busy to keep up. Close other
    programs, or run fewer receivers.
  - **Clock not NTP synchronised**: the computer's clock is not set from the
    internet. Spot times will be off; turn on automatic time.
- **A stopped receiver** shows its last error under its name. Turn **RUN**
  on; if it stops again, send the message to whoever set it up.

On a FlexRadio, Details also show the receiver's slice (with the letter
SmartSDR shows), its antenna and RF gain as the radio has them, and its
DAX-IQ channel.

## This browser

- **Theme**: the sun button at the top of ⚙, dark or light.
- **Decoded text**: in each receiver's settings; it applies to every
  receiver in this browser.

## What's new

- **2026-11-05.** The ⚙ dialog is Radios: each radio with its node, feeder
  and receivers, and **+ Add a receiver** under it; Remove keeps a receiver's
  settings for the next Add. The Radio line and the bottom bar say
  **Connecting**, **Connected**, **Reconnecting** or **Not connected** as the
  radio is, and the server's log has every warning and error from the
  receivers, station and feeder. Receivers added here take telnet 7330 plus
  their DAX-IQ channel, clear of the RBN Aggregator's 7300.
- **2026-11-04.** SparkGap follows the radio: a running skimmer takes antenna,
  RF gain and loop changes from here, SmartSDR, SmartControl or the radio, and
  keeps them across a reconnect. Save Config is gone.

- **2026-10-28.** The radio card follows the radio: antenna, RF gain and
  loops changed in SmartSDR, Maestro or by another skimmer show within a
  couple of seconds. **Startup Config** shows what a skimmer starts with, and
  **Save Config** saves the radio's current values. A change here no longer
  changes the config by itself. Info shows the slice, antenna and DAX-IQ
  channel; a stopped skimmer shows its cards empty. AFC moved to the top card.
  Removing a server asks first.
- **2026-10-27.** The surer of the two readings is in bold, and a reading that
  copied nothing says so. AFC can be switched per skimmer. Each skimmer keeps
  the signal you last picked on it. Ctrl+F finds text inside the ⚙ dialog.
  A band change clears the old signal. The ⚙ dialog has its own **?** for
  help and this list. A skimmer's settings are in three cards (the skimmer
  with Apply, Radio, Decoder Options) with more of the decoder's options, and
  the Servers tab starts and stops skimmers.
- **2026-10-26.** Spots, not consoles, in the bottom bar; a band change
  counts spots and calls from zero.

## Setting it up

Installing SparkGap and its server, addresses, join codes and remote access are
in [SERVER_SETUP.md](SERVER_SETUP.md). Sending spots to RBN:
[sending-spots.md](sending-spots.md).
