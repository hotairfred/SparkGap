# SparkGap server setup

For whoever installs SparkGap: how the server runs a radio's receivers, how
SparkGap Web reaches them, and the settings involved. Operators only need the
page's address and the join code (see [WEBUI.md](WEBUI.md)).

## How it fits together

The SparkGap server (`sparkgap_server.py`) runs on the machine beside the
radio. It serves SparkGap Web and runs everything else:

- a **receiver** for each band: one `sparkgap.py` process, named
  `<radio>-<n>` (`Dallas-1`; on FlexRadio `n` is its DAX-IQ channel);
- a **station** for each FlexRadio: SparkGap's one connection to the radio,
  which its receivers share (`SparkGap-Dallas` in SmartSDR's client list);
- a **feeder** for each radio: sends the spots of all its receivers to RBN.

The server starts a radio's station and feeder with its first receiver and
stops them after its last. Browsers talk only to the server; the receivers'
own feeds stay on the machine. More in [RADIOS.md](RADIOS.md).

## Setting up a machine

From a SparkGap clone:

```text
tools/sparkgap.sh setup
```

`setup` makes the run folder (`~/sparkgap`, or `$SPARKGAP_ROOT`), clones the
code into it, starts the server, finds the radio and asks for the spotter ID
and grid. Where broadcasts do not reach the machine it asks for the radio's
address. Run it again any time: it asks only what is missing. Then add
receivers in SparkGap Web (**⚙**, **+ Add a receiver**).

```text
~/sparkgap/
  server.json                 the server's address, port and join code
  instances                   one line per receiver, written by Add
  config/Dallas.radio.json    the radio: address, model, spotter ID, grid, RBN upload
  config/Dallas-1.local.json  a receiver: band, receive path, decoder options
  code/                       the SparkGap code the server and receivers run
  logs/                       the server's log and each process's own
  rec/                        recordings, when turned on
```

Day to day, `sparkgap.sh` has four commands:

- `setup`: as above.
- `start`: the server, and the receivers that ran at the last `stop`.
- `stop`: every receiver (remembered for `start`), its station and feeder,
  then the server. Stopping the server alone leaves receivers running.
- `status`: each radio, its node and feeder, and its receivers.

`start` or `stop` with a receiver's name acts on that one. Everything else is
in SparkGap Web. The command line client, `python3 sparkgap_ctl.py`
(`status`, `start`, `stop`, `restart`, `band`, `rec`, `rbn`, `set`,
`discover`), drives the server as SparkGap Web does; `sparkgap.sh` uses it
for `start`, `stop` and `status`.

## Adding and removing receivers

**+ Add a receiver** under a radio in SparkGap Web picks a free DAX-IQ
channel and a band, shows the recording and decoder options, and on Apply
writes the receiver's config and `instances` line and starts it. Add checks
that the radio file has a spotter ID and grid and that the radio has a pan,
slice and DAX-IQ channel to spare.

**Remove** stops the receiver and takes it out of `instances`. Its config,
logs and recordings stay. Adding the same name again brings back its
settings, with new ports.

## Ports

SparkGap keeps to 7320-7339, clear of the RBN Aggregator's 7300:

- 7320: the server and SparkGap Web.
- 7321-7329: the receivers' own feeds (on this machine only).
- 7330 + DAX-IQ channel: a receiver's telnet spots (`Dallas-1` 7331).
- 7790 + DAX-IQ channel: a receiver's DAX-IQ stream from the radio (UDP).

Add assigns the telnet and DAX-IQ ports, taking the next one free on the
machine when one is in use. A receiver added before these rules keeps its old
ports until it is removed and added again.

## Radio status

SparkGap Web's Radio bar and `sparkgap.sh status` show the radio's
connection as its station reports it:

- **Connecting** (amber): the station is starting and has not reached the
  radio yet.
- **Connected** (green): the station holds the radio.
- **Reconnecting** (amber): the radio was lost (rebooted, off the network).
  The station tries for up to 10 minutes and registers again once the radio
  has answered steadily for 60 seconds; the receivers follow.
- **Not connected** (red): no receiver runs on the radio, or the station gave
  up (the radio stayed away 10 minutes, or was lost a fourth time within
  an hour) and
  leaves the radio alone. Fix the radio, then start the receivers again.

The bottom bar says the same for the receiver on screen: **Reconnecting to
radio** or **Radio not connected** while its feed is live but its radio is
not.

## When something is wrong

- **The server's log** (the Log section of the Server pane in SparkGap Web,
  and `logs/server_<UTC>.log`): requests and who made them, results and
  refusals, radio state changes (`Server: radio Dallas reconnecting`), and
  every warning and error from each receiver, station and feeder
  (`Station Dallas: ...`, `Skimmer Dallas-2: ...`, `Feeder Dallas: ...`).
- **A receiver's Details** in SparkGap Web: its packets, reconnects, and
  recent warnings and errors.
- **Each process's own log**, in `logs/`: `<receiver>_<band>_<UTC>.log`,
  `station_<radio>_<UTC>.log`, `rbn2_<radio>_<UTC>.log`.

## server.json

```json
{"bind": "127.0.0.1", "port": 7320, "join_code": "XXXXXX"}
```

- `bind`: the address to listen on. `127.0.0.1` serves only this machine; set
  the machine's LAN or VPN address so other machines can reach it. A browser
  on the server's own machine can then also use `localhost` or the machine's
  name: the server listens on those too.
- `port`: 7320 (see Ports above).
- `join_code`: written at first start. Five wrong codes from one address lock
  that address out for 10 minutes. The code also grants control: anyone with it
  can start, stop and retune the receivers.
- `tls_cert` and `tls_key` (optional): serve `https://` and `wss://`. Without
  them the page and the code cross the network unencrypted.

For access from outside the LAN, use a VPN rather than opening the port to the
internet.

**Windows with WSL:** a browser in Windows reaches a server in WSL at
`http://localhost:7320/`. To use the machine's name or address instead, set
mirrored networking with host address loopback in `%USERPROFILE%\.wslconfig`,
then restart WSL (`wsl --shutdown`):

```ini
[wsl2]
networkingMode=mirrored

[experimental]
hostAddressLoopback=true
```

## A receiver's feed: gui_feed

A receiver's feed is off unless its config has a `gui_feed` block. Add
writes the shipped one:

```json
"gui_feed": {"bind": "127.0.0.1", "port": "auto", "web": true}
```

- `bind`: keep `127.0.0.1` behind the server. The server passes through only
  feeds on this machine, so their codes never cross the network.
- `port`: `"auto"` (default) takes the first free port from 7321 to 7329.
- `join_code`: required whenever `bind` is not `127.0.0.1`. Six characters,
  minted without pairs easily confused by eye or over the air (O/0, I/1 and
  similar); case, spaces and dashes are ignored on entry. Make one with
  `python3 feed_server.py --new-code`. SparkGap logs the code at startup.
- `web`: `true` also serves SparkGap Web on the feed's own port, showing that
  receiver alone. This is how a replay (`--file`) is watched without a server.
- `spots`: which spots the page shows. `"rbn"` (default) is what `telnet_port`
  carries; `"local"` is what `telnet_local_port` carries, with its own
  `local_spot_rule`.
- `tls_cert` and `tls_key` (optional): serve `wss://`.
- At most 16 browsers at once; one that stops reading is disconnected.

## Live band change

A band change alone moves a running FlexRadio receiver in place, without a
restart. It falls back to a restart while recording, with HPSDR, with more than
one band, or with per-band settings (`per_band_min_snr`, `pfb_scanner_bands`).
The new band is saved as the receiver's startup band.

## Cost

With one or two browsers watching, the feed costs about 5% of one core (96 kHz
on a Ryzen 5 5500U), and nothing while no browser is connected.

## Sending spots to RBN

Each radio's feeder needs the RBN auth library in the run folder
(`~/sparkgap/librbn2_auth.so`) before RBN upload is set to live:
[sending-spots.md](sending-spots.md).

## For developers

What the server and the feeds send: [SERVER_PROTOCOL.md](SERVER_PROTOCOL.md) and
[GUI_FEED_PROTOCOL.md](GUI_FEED_PROTOCOL.md).
