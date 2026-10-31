# GUI feed protocol

What a running SparkGap sends to its clients when the config has a `gui_feed`
block: decoded windows, spots, status and spectrum rows over one WebSocket.
SparkGap Web is one client; anything that speaks WebSocket can be another.
Setup and the config block are in [SERVER_SETUP.md](SERVER_SETUP.md).

Protocol `sparkgap.v1`, feed version 1.

## Connecting

One endpoint per SparkGap: `ws://<host>:<port>/feed` (the port is the first free
one from 7321 to 7339 unless the config sets one; 7320 is the machine's server's,
which passes the feed through as `/feed/<name>`), or `wss://` when the config
has `tls_cert` and `tls_key`. A standard WebSocket handshake (RFC 6455,
version 13):

- Path `/feed`; a query string is ignored.
- `Sec-WebSocket-Protocol: sparkgap.v1` is optional. When offered, the server
  echoes it.
- `Authorization: Bearer <join code>` whenever the feed has a join code, which
  it must have unless it is bound to a loopback address. Case, spaces and
  dashes are ignored, so `xxx-xxx` and `XXXXXX` are the same code.
- A browser cannot set that header, so it offers the code as a second
  subprotocol instead: `sparkgap.code.<join code>`, after `sparkgap.v1`. The
  server echoes only `sparkgap.v1`.
- A feed without a join code accepts a request with an `Origin` header only
  when that origin is a loopback address (`localhost`, `127.0.0.1`, `::1`) on
  the feed's own port or on 7321-7339 (another copy's page, which lists this
  copy too), so a web page from elsewhere cannot read a feed on this machine,
  even through a name that resolves to 127.0.0.1.

The server refuses a connection with a plain HTTP reply and closes it:

| Status | When |
| --- | --- |
| 400 | Not a WebSocket upgrade, or not version 13 |
| 401 | Wrong or missing join code |
| 403 | No join code, and the `Origin` is not an allowed loopback page |
| 404 | Path is not `/feed` |
| 405 | Not a `GET` |
| 429 | This address gave 5 wrong codes in the last 10 minutes |
| 431 | Request headers over 8 KB |
| 503 | 16 clients are already connected |

A 401 should not be retried with the same code. A 429 clears as the oldest
wrong try passes 10 minutes. A missing code is refused but does not count as a
wrong try.

A browser cannot read an HTTP refusal, so a request with an `Origin` header is
refused differently: the server completes the handshake, then closes at once
with one of these codes.

| Close code | When |
| --- | --- |
| 4401 | Wrong or missing join code (HTTP 401) |
| 4429 | Locked out after wrong codes (HTTP 429) |
| 4503 | Too many clients (HTTP 503) |

Other refusals (400, 403, 404, 405, 431) are HTTP replies for browsers too.

With `"web": true` in the `gui_feed` block, other `GET` paths on the same port
serve SparkGap Web, the browser console: the files in `web/` at `/`, and the
console's fonts and icons at `/fonts/` and `/icons/`. Anything else is a 404.

## Messages from the server

Text frames carry one JSON object each, with a `type`. Binary frames carry
spectrum rows. Frequencies are kHz, times are Unix seconds (UTC) as floats, SNR
is dB. A value that is unknown is `null`, never `0`.

On connect the server sends `hello`, then `snapshot`, then live messages as
they happen.

### hello

Who this SparkGap is and the settings it runs with.

```json
{"type": "hello", "v": 1, "host": "shack-pc", "build": "06e710d",
 "call": "W1AW", "decoder": "itila2",
 "config": {"callsign": "W1AW", "cw_decoder": "itila2", "itila2_afc": false,
            "sample_rate": 96000, "flex_daxiq_channel": 2,
            "telnet_port": 7300, "enable_ft8": false,
            "spot_rule": "repeat", "spot_rule_cq_groups": true,
            "spot_rule_lockon": true, "spot_rule_cq_qualifiers": true,
            "spot_rule_split_runs": true, "spot_rule_contest_words": false,
            "spot_rule_cq_repeats": false, "spot_rule_doubled_calls": false,
            "spot_rule_split_up": false, "spot_rule_runner_signs": false,
            "spot_rule_fragments": "off",
            "spot_rule_owner_cell": false, "spot_rule_owned_two_windows": false,
            "itila_window_sec": 30.0, "itila_max_bins": 400,
            "itila_decode_threads": 1, "spot_validation": "normal",
            "spot_validation_calls": "rbn+skcc", "rbn_known_days": 7},
 "config_defaults": ["rbn_known_days"],
 "config_built_in": ["spot_rule_cq_groups", "spot_rule_lockon",
                     "spot_rule_cq_qualifiers"],
 "bands": [{"name": "40m", "center_khz": 7048.0, "span_khz": 96.0}],
 "run": {"config": "/srv/sparkgap/station.local.json",
         "log": "/srv/sparkgap/logs/sparkgap.log", "started": 1792020000}}
```

- `v`: feed version.
- `host`: the host name of the machine SparkGap runs on.
- `build`: the commit SparkGap runs from (`git describe`), `null` outside a git
  checkout.
- `config`: settings in effect, including defaults SparkGap applied.
  `flex_daxiq_channel` is `null` off a Flex; `rbn_known_days` is `null` unless
  the known calls come from RBN.
- `config_defaults`: keys of `config` not set in the config file.
- `config_built_in`: keys that follow from another setting (the repeat rule)
  rather than being set on their own.
- `bands`: one entry per receiver, in the order of the config's `bands`.
- `run`: the config file SparkGap was started with, the file its log goes to
  (`null` when the log goes to a terminal), and the start time (UTC seconds).

### snapshot

What happened before the client connected.

```json
{"type": "snapshot",
 "signals": [{"khz": 7021.2, "wpm": 22.0, "snr": 18.0, "utc": 1792020908.1}],
 "windows": [{"type": "window", "khz": 7021.2, "text": "CQ DE W1AW W1AW K",
              "wpm": 22.0, "snr": 18.0, "cost": 0.4, "utc": 1792020908.1}],
 "spots": [{"type": "spot", "khz": 7021.2, "call": "W1AW", "snr": 18.0, "wpm": 22,
            "utc": 1792020909.4}],
 "status": null}
```

- `signals`: one per frequency with a window in the last 2 minutes, from its
  latest window.
- `windows`: every decoded window of the last 10 minutes, oldest first.
- `spots`: the last 200 spots, oldest first.
- `status`: the latest status, or `null` before the first one.

### window

One decoded window of one signal: all the text, not only calls.

```json
{"type": "window", "khz": 7021.2, "text": "CQ DE W1AW W1AW K", "wpm": 22.0,
 "snr": 18.0, "cost": 0.4, "utc": 1792020908.1}
```

- `khz`: to 0.01 kHz.
- `cost`: the decoder's fit, lower is better; `null` when the decoder gives none.

An active signal sends windows repeatedly, and they overlap: the text of one
can repeat in the next.

### spot

A spot, as sent to telnet.

```json
{"type": "spot", "khz": 7021.2, "call": "W1AW", "snr": 18.0, "wpm": 22,
 "utc": 1792020909.4}
```

### status

Every 2 seconds.

```json
{"type": "status", "cpu": 38, "rss_mb": 331, "clock_synced": true,
 "bins": 37, "spots": 412, "uptime_s": 27243, "utc": 1792020910.0,
 "radio": {"type": "flex", "address": "192.168.1.20", "state": "live",
           "packets": 5023532, "lost": 0, "reconnects": 1,
           "last_reconnect": 1792019016, "station": "SparkGap-2",
           "daxiq_channel": 2, "others": ["Shack (SmartSDR-Win)"],
           "muted_packets": 0, "slice": 1, "slice_letter": "B",
           "rx": {"rxant": "ANT1", "loopa": false, "loopb": false, "rfgain": 8,
                  "ant_list": ["ANT1", "ANT2", "RX_A", "XVTA"],
                  "rfgain_range": [-8, 32, 8]}},
 "telnet": {"port": 7300, "clients": 1}, "iq_trimmed_s": 0.0,
 "recording": {"file": "/srv/sparkgap/rec/40m.wav", "bytes": 2473963564},
 "windows": 72823, "calls": 97, "spots_hour": 41, "bins_peak": 52,
 "decode_khz": [[7000.0, 7070.0]],
 "clients": [{"peer": "192.168.1.31:61718", "name": "shack", "via": null,
              "since": 1792018739, "dropped": 12}],
 "problems": {"errors": 8,
              "last": [{"utc": 1792019013, "level": "ERROR",
                        "msg": "[Flex] The radio closed the control connection"}]}}
```

Process and decoding:

- `cpu`: percent of one core, averaged over the last minute (all threads).
- `rss_mb`: resident memory; `null` where the OS does not report it.
- `clock_synced`: whether the system clock is NTP-synchronised; `null` where
  that cannot be read.
- `bins`: decoder bins open across all bands; `bins_peak`: the most since start.
- `decode_khz`: each band's decode window `[low, high]` in kHz, in `bands` order;
  `null` until the band's first IQ arrives, and for a band not on the ITILA
  scanner.
- `spots`: spots since start; `spots_hour`: in the last hour; `calls`: distinct
  calls spotted since start.
- `windows`: decoded windows since start.
- `iq_trimmed_s`: seconds of IQ dropped before the decoder because it fell
  behind; above zero means the machine is short of CPU.

The station (absent in file mode except `radio`):

- `radio`: `type` (`flex`, `hpsdr` or `file`), `address` (the radio's address,
  or the file being replayed) and `state` (`connecting`, `live`, `reconnecting`,
  `waiting` or `replaying`). A Flex adds `packets`, `lost`, `reconnects`,
  `last_reconnect` (UTC seconds or `null`), its `station` name, its
  `daxiq_channel`, the `others` stations on the radio at the last connect,
  how many packets it has silenced while the radio was keyed
  (`muted_packets`), our slice's number (`slice`) and the letter SmartSDR shows
  for it (`slice_letter`), and
  `rx`: our pan's receive path as the radio reports it (`rxant`, `loopa`,
  `loopb`, `rfgain`, `null` when the radio reports none; a loop only on a
  model with its jacks: Loop A on the FLEX-6500, 6700 and 6700R, Loop B on the
  6700 and 6700R), the antennas the pan offers (`ant_list`) and its RF gain
  `[low, high, step]` (`rfgain_range`).
  The HPSDR C receiver adds `packets`, `lost` and `pkt_per_s`; file mode adds
  `position_s`.
- `telnet`: the spot port and how many telnet clients are connected.
- `recording`: the IQ file being written and its size, or `null`.

The feed itself:

- `clients`: each connected console: address, host name from reverse DNS
  (`null` when it has none), `via` (`"server"` for a browser the SparkGap
  server passes through, its address taken from the server; else `null`),
  connected since (UTC seconds), spectrum rows dropped because it fell behind.
- `problems`: SparkGap's errors since start, with the last five
  (UTC seconds, level, message).

### Spectrum rows (binary)

About 60 rows a second per band, thinned to the rate each client asks for
(20 by default). Little-endian:

| Offset | Type | Field |
| --- | --- | --- |
| 0 | u32 | feed version |
| 4 | f64 | `utc` of the row's last sample |
| 12 | f64 | `first_bin_hz`: frequency of bin 0 |
| 20 | f32 | `hz_per_bin` |
| 24 | f32 | `db_per_unit` |
| 28 | u16 | `n_bins` |
| 30 | u8 × `n_bins` | level of each bin |

Bin `i` is at `first_bin_hz + i * hz_per_bin` and its level is
`value * db_per_unit` dB over the local noise floor (0.25 dB steps today, so
0 to 63.75 dB). With several bands, `first_bin_hz` tells them apart. Rows from
one block of IQ arrive together; space them by `utc`, not arrival time.

Clients should check the version, that the frame is 30 + `n_bins` bytes long,
and that the floats are finite.

## Messages from the client

Text frames of JSON, up to 64 KB. Two messages exist today.

### subscribe

```json
{"type": "subscribe", "spectrum_hz": 20}
```

`spectrum_hz` sets this client's spectrum rows per second, from 0.5 to 60.
Anything that is not valid JSON, or has an unknown `type`, is ignored. A binary
frame closes the connection.

### control

Changes the running SparkGap, live.

```json
{"type": "control", "id": 5, "set": {"band": "20m"}}
```

- `id`: a string or an integer, up to 64 characters. A message with no usable
  `id` gets no answer.
- `set`: a non-empty object, else the answer is the error `set must be an
  object`. Either `band` (a band name such as `40m`) or `rx`, our pan's
  receive path: any of `rxant` (one of the pan's `ant_list`), `loopa` and
  `loopb` (true or false, only on a model that has that loop) and
  `rfgain` (in the radio's `rfgain_range` steps), as in
  `{"rx": {"rxant": "ANT2", "rfgain": 16}}`.

The answer goes only to the client that asked:

```json
{"type": "control_result", "id": 5, "ok": true, "message": "retuned to 20m"}
```

A refusal has `"ok": false` and `error` in place of `message`.

A live band change works for a FlexRadio copy running live with one band, not
recording (`record_wav`), and without per-band settings (`per_band_min_snr`,
`pfb_scanner_bands`). Otherwise the error says why: `not supported` (HPSDR,
file replay), `only the band can be changed live`, `not supported with more
than one band`, `not supported while recording`, `unknown band '6m'`, or `not
supported with per-band settings`. An `rx` change works for a FlexRadio copy
running live; a value the radio does not offer is refused before anything is
sent, and a change the radio refuses is put back to what it reported before.
Loops are shared by every pan on that antenna and RF gain by every pan on the
same SCU, so either can also change under the copy; `status` shows what the
radio reports. One change runs at a time: another sent
meanwhile is answered `busy: a change is under way`. A copy already on that
band answers ok with `already on 40m`. While the copy is recovering from a radio
outage, a change is refused within about 2 seconds with an error starting
`recovering`, rather than waiting out the outage.

On success the radio's pan and slice move first. If the radio refuses, nothing
changes and its error is returned; if the old frequency cannot be confirmed
after a failure, the copy stops rather than decode a frequency it does not know.
Then the copy drops the old band's IQ, decoders and waterfall, and every
connected client gets a new `hello` (with the new `bands`) followed by an empty
`snapshot`. Queued old-band messages and spectrum rows are dropped first, so
nothing from the old band arrives after the new `hello`. A client should treat a
`hello` in mid-connection as a fresh start.

- The change is not written to the copy's config by the copy: it lasts until the
  copy restarts. (The SparkGap server writes the startup band, see
  [SERVER_PROTOCOL.md](SERVER_PROTOCOL.md).)
- The spot tracker is not reset. Spots on another band do not collide in
  frequency; with `gate_harmonic_filter` on (default off), a call spotted on the
  old band can hold back a spot of the same call at a harmonic frequency on the
  new band for the gate's window.
- The copy's own join code grants control: anyone who can open the feed with it
  can change the band. A feed on loopback with no join code can be changed by
  any local program. The SparkGap server keeps the copy's code on its machine
  and opens the feed as `/feed?view&for=<browser address>` for a browser: a
  `control` sent on such a connection is refused with `view only: settings
  change through the server`. The copy takes `for` only on a view-only feed
  opened from its own machine.
  Browsers change settings through the server's own commands.

## Keeping the connection

- The server pings every 20 seconds. A client that sends nothing for 60
  seconds, not even the pong every WebSocket library sends, is disconnected.
- A client that falls 5,000 text messages behind is disconnected. Spectrum
  rows are not queued behind it: each client holds the 4 newest and the oldest
  are dropped first. Windows and spots are never dropped.
- Close codes from the server: 1001 when SparkGap stops, 1002 for a protocol
  error (such as an unmasked or badly fragmented frame), 1003 for a binary
  frame, 1009 for a message over 64 KB.

## Versioning

New fields and new message types are added without changing the version, so
clients must ignore fields and types they do not know. Removing or renaming a
field, or changing what one means, raises the feed version and the protocol
name (`sparkgap.v2`).
