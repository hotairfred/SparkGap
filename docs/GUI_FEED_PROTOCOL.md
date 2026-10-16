# GUI feed protocol

What a running SparkGap sends to its clients when the config has a `gui_feed`
block: decoded windows, spots, status and spectrum rows over one WebSocket.
SparkGap Console is one client; anything that speaks WebSocket can be another.
Setup and the config block are in [CONSOLE.md](CONSOLE.md).

Protocol `sparkgap.v1`, feed version 1.

## Connecting

One endpoint per SparkGap: `ws://<host>:7320/feed`, or `wss://` when the config
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
  the feed's own port, so a web page from elsewhere cannot read a feed on this
  machine, even through a name that resolves to 127.0.0.1.

The server refuses a connection with a plain HTTP reply and closes it:

| Status | When |
| --- | --- |
| 400 | Not a WebSocket upgrade, or not version 13 |
| 401 | Wrong or missing join code |
| 403 | No join code, and the `Origin` is not loopback on this port |
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
 "config": {"callsign": "W1AW", "cw_decoder": "itila2",
            "sample_rate": 96000, "flex_daxiq_channel": 2,
            "telnet_port": 7300, "enable_ft8": false,
            "spot_rule": "repeat", "spot_rule_cq_groups": true,
            "spot_rule_lockon": true, "spot_rule_cq_qualifiers": true,
            "spot_rule_split_runs": true},
 "config_defaults": ["telnet_port"],
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
  `flex_daxiq_channel` is `null` off a Flex.
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
 "radio": {"type": "hpsdr", "address": "192.168.1.20", "state": "live",
           "packets": 5023532, "lost": 0, "pkt_per_s": 3810},
 "telnet": {"port": 7300, "clients": 1}, "iq_trimmed_s": 0.0,
 "recording": {"file": "/srv/sparkgap/rec/40m.wav", "bytes": 2473963564},
 "windows": 72823, "calls": 97, "spots_hour": 41, "bins_peak": 52,
 "clients": [{"peer": "192.168.1.31:61718", "since": 1792018739,
              "dropped": 12}],
 "problems": {"warnings": 2, "errors": 8,
              "last": [{"utc": 1792019013, "level": "ERROR",
                        "msg": "hpsdr_create failed"}]}}
```

Process and decoding:

- `cpu`: percent of one core since the last status.
- `rss_mb`: resident memory; `null` where the OS does not report it.
- `clock_synced`: whether the system clock is NTP-synchronised; `null` where
  that cannot be read.
- `bins`: decoder bins open across all bands; `bins_peak`: the most since start.
- `spots`: spots since start; `spots_hour`: in the last hour; `calls`: distinct
  calls spotted since start.
- `windows`: decoded windows since start.
- `iq_trimmed_s`: seconds of IQ dropped before the decoder because it fell
  behind; above zero means the machine is short of CPU.

The station (absent in file mode except `radio`):

- `radio`: `type` (`flex`, `hpsdr` or `file`) and `address` (the radio's
  address, or the file being replayed). The HPSDR C receiver adds `state`
  (`live` or `waiting`), `packets`, `lost` and `pkt_per_s`; file mode adds
  `state` (`replaying`) and `position_s`.
- `telnet`: the spot port and how many telnet clients are connected.
- `recording`: the IQ file being written and its size, or `null`.

The feed itself:

- `clients`: each connected console: address, connected since (UTC seconds),
  spectrum rows dropped because it fell behind.
- `problems`: SparkGap's warnings and errors since start, with the last five
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

Text frames of JSON, up to 64 KB. One message exists today:

```json
{"type": "subscribe", "spectrum_hz": 20}
```

`spectrum_hz` sets this client's spectrum rows per second, from 0.5 to 60.
Anything that is not valid JSON, or has an unknown `type`, is ignored. A binary
frame closes the connection.

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
