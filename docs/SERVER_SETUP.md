# SparkGap server setup

For whoever installs SparkGap: how SparkGap Web reaches the skimmers, and the
settings involved. Operators only need the page's address and the join code
(see [WEBUI.md](WEBUI.md)).

## How it fits together

Each skimmer is one SparkGap process, one band from one radio. The SparkGap
server (`sparkgap_server.py`) runs on the same machine. It starts and stops the
skimmers, changes their settings, serves SparkGap Web, and passes each
skimmer's live feed through to the browser. Browsers talk only to the server;
the skimmers' own feeds stay on the machine.

## The run folder

The server runs the skimmers listed in its run folder (`~/sparkgap`, or
`--root` / `$SPARKGAP_ROOT`). Set it up once:

```text
~/sparkgap/
  instances             one line per skimmer
  config/shack40.local.json
  config/shack20.local.json
  logs/                 the server and each skimmer write here
  rec/                  recordings, when turned on
```

`instances` has one line per skimmer: its name, the SparkGap folder it runs
from, its config file in `config/`, and the Python to run it with. Lines
starting with `#` are ignored.

```text
# name   code folder          config               python
shack40  /home/op/SparkGap    shack40.local.json   /home/op/SparkGap/.venv/bin/python
shack20  /home/op/SparkGap    shack20.local.json   /home/op/SparkGap/.venv/bin/python
```

Each config is an ordinary SparkGap config (start from
`skimmer_flex.example.json`). Skimmers on one FlexRadio need their own
`flex_daxiq_channel`, `flex_udp_port`, `telnet_port` and `skimmer_suffix`;
give each a `gui_feed` block (below) so it shows in SparkGap Web. A name is a
letter then letters, digits, `-` or `_`.

## Starting the server

```text
python3 sparkgap_server.py
```

At its first start it writes `server.json` in the run folder (`~/sparkgap`,
or `--root` / `$SPARKGAP_ROOT`) with a new join code, and prints the code.
Give operators `http://<address>:7320/` and that code.

## server.json

```json
{"bind": "127.0.0.1", "port": 7320, "join_code": "XXXXXX"}
```

- `bind`: the address to listen on. `127.0.0.1` serves only this machine; set
  the machine's LAN or VPN address so other machines can reach it.
- `port`: 7320. The skimmers' own feeds take 7321 to 7339.
- `join_code`: written at first start. Five wrong codes from one address lock
  that address out for 10 minutes. The code also grants control: anyone with it
  can start, stop and retune the skimmers.
- `tls_cert` and `tls_key` (optional): serve `https://` and `wss://`. Without
  them the page and the code cross the network unencrypted.

For access from outside the LAN, use a VPN rather than opening the port to the
internet.

## A skimmer's feed: gui_feed

A skimmer's feed is off unless its config has a `gui_feed` block:

```json
"gui_feed": {"bind": "127.0.0.1", "port": "auto", "join_code": null, "web": false}
```

- `bind`: keep `127.0.0.1` behind the server. The server passes through only
  feeds on this machine, so their codes never cross the network.
- `port`: `"auto"` (default) takes the first free port from 7321 to 7339.
- `join_code`: required whenever `bind` is not `127.0.0.1`. Six characters,
  minted without pairs easily confused by eye or over the air (O/0, I/1 and
  similar); case, spaces and dashes are ignored on entry. Make one with
  `python3 feed_server.py --new-code`. SparkGap logs the code at startup.
- `web`: `true` also serves SparkGap Web on the feed's own port, showing that
  skimmer alone. This is how a replay (`--file`) is watched without a server.
- `spots`: which spots the page shows. `"rbn"` (default) is what `telnet_port`
  carries; `"local"` is what `telnet_local_port` carries, with its own
  `local_spot_rule`.
- `tls_cert` and `tls_key` (optional): serve `wss://`.
- At most 16 browsers at once; one that stops reading is disconnected.

## Live band change

A band change alone moves a running FlexRadio skimmer in place, without a
restart. It falls back to a restart while recording, with HPSDR, with more than
one band, or with per-band settings (`per_band_min_snr`, `pfb_scanner_bands`).
The new band is saved as the skimmer's startup band.

## Cost

With one or two browsers watching, the feed costs about 5% of one core (96 kHz
on a Ryzen 5 5500U), and nothing while no browser is connected.

## For developers

What the server and the feeds send: [SERVER_PROTOCOL.md](SERVER_PROTOCOL.md) and
[GUI_FEED_PROTOCOL.md](GUI_FEED_PROTOCOL.md).
