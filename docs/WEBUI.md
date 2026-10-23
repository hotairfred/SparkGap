# SparkGap Web (experimental)

SparkGap Web shows your SparkGap skimmers in a web browser. You see the band
as a waterfall, every call SparkGap hears marked by frequency, and the CW it
copies from the station you pick. From the same page you can start and stop a
skimmer, move it to another band, and change how it spots. Nothing to install:
any browser on your network will do, and closing the page does not affect the
skimmers.

![SparkGap Web on 40m](web.png)

## Getting connected

You need two things from whoever set up SparkGap on the radio's computer: the
page's address (for example `http://192.168.1.20:7320/`) and a six-character
join code (for example `KTR-49X`).

1. Open the address in your browser.
2. Click **+ Add server**, enter the address and the join code, and save.
3. The waterfall starts. If the computer runs more than one skimmer, pick one
   from the band list at the top.

The page remembers the server, so next time it opens straight to the
waterfall. A wrong code five times in a row locks your browser out for ten
minutes.

## The main screen

- **Waterfall**: the whole band the skimmer listens to. Signals are the bright
  traces.
- **Call ladder**, beside the waterfall: every signal by frequency. Spotted
  calls stand out, calls heard but not spotted are dimmer, and signals with no
  call yet show only their frequency.
- **Decoded text**, on the right: click a trace or a call to see what SparkGap
  copied from that station, newest first, with its frequency, speed and signal
  strength. You can select the text, or **Copy** all of it.
- **Search** (Ctrl+F): type a call, some text or a frequency in kHz to jump to
  it.
- **Zoom**: drag to move along the band; Ctrl+scroll or pinch to zoom. The
  **Zoom** button steps through the whole band, half and a quarter.
- **Bottom bar**: how hard the computer is working, how many browsers are
  watching, and how long the skimmer has been running.

## Running a skimmer

Click **⚙**, then the skimmer's tab (its name, such as `shack40`).

- **Start**, **Stop** and **Restart** do what they say. **Show** puts that
  skimmer on the main screen.
- To change a setting, pick the new value and click **Apply**. You can change
  several at once; they take effect together. **Discard** forgets them.
- **Band**: changes straight away while the skimmer runs, in about a second,
  and the waterfall starts over on the new band. Other settings restart the
  skimmer, which takes about half a minute.

Settings, in plain terms:

- **Recording**: saves the raw signal to disk so it can be replayed later. It
  uses a lot of space (it needs at least 5 GB free); leave it off unless you
  were asked to record.
- **Call validation**: how sure SparkGap must be before it spots a call.
  Permissive spots more calls but gets more wrong; aggressive spots fewer but
  surer ones. Normal suits most stations.
- **Known call list**: calls on this list count as more likely to be right.
  SCP is the contest call list; RBN adds calls other RBN skimmers heard in the
  past week; RBN + SKCC adds the SKCC roster, good for hand-keyed CW.
- **Advanced**: three options that spot more calls, each with some risk of a
  wrong one. All off unless you need them.
  - **Split runs**: reads `CQCQDE` as `CQ CQ DE`, for fists that run words
    together.
  - **Contest words**: treats CWT, SST or MST before a call like CQ, for
    runners who skip the CQ.
  - **CQ repeats**: spots a station sooner when it sends CQ several times in a
    row.

Hover over a setting's name for a one-line reminder.

## When something looks wrong

Click **⚙**, then **Info**. Each skimmer's tab has a line at the top: green
means the radio is connected and decoding keeps up. Otherwise it says what is
wrong, worst first. Common ones:

- **Radio reconnecting** or **Radio reconnected**: SparkGap lost the radio for
  a moment, usually a network hiccup, and reconnects by itself. If it stays
  down, check the radio and its network cable.
- **Decoder behind**: the computer is too busy to keep up. Close other
  programs, or run fewer skimmers.
- **Clock not NTP synchronised**: the computer's clock is not set from the
  internet. Spot times will be off; turn on automatic time.
- **A stopped skimmer**: its tab shows why. Try **Start**; if it stops again,
  send the message to whoever set it up.

The **Servers** tab under Info shows the server's log: every start, stop and
setting change, with its result. It is the first thing to look at, and the
thing to send along when asking for help.

## This browser

Under **⚙**, **Settings**, **Servers**:

- **Theme**: dark or light.
- **Decoded text**: SparkGap reads each signal twice, through a wide and a
  narrow filter. **Lowest cost** (the default) shows whichever it is surer of;
  you can show both, or only one.

## Setting it up

Installing SparkGap and its server, addresses, join codes and remote access
are in [SERVER_SETUP.md](SERVER_SETUP.md).
