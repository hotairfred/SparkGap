# SparkGap server protocol

The server (`sparkgap_server.py`) runs one machine's SparkGap instances for
SparkGap Web. It listens on one port, 7320 by default, and serves three things
there:

- `/server`: this protocol, a WebSocket.
- `/feed/<name>`: the feed of running instance `<name>`, passed through
  unchanged. The protocol is the feed's own (`GUI_FEED_PROTOCOL.md`). The server
  opens the copy's feed on its own machine with the copy's join code, so the
  copy's feed can stay on loopback and its code never leaves the machine.
- Any other path: SparkGap Web.

Instances on `"port": "auto"` take the feed ports after the server's, from 7321
to 7329. A receiver's telnet port is 7330 plus its DAX-IQ channel and its UDP
port 7790 plus the channel, or the next one free on the machine.

## Settings

`<root>/server.json`, written on first run with a new join code and readable by
its owner only. The root is `--root`, else `$SPARKGAP_ROOT`, else `~/sparkgap`,
laid out as in [SERVER_SETUP.md](SERVER_SETUP.md) (`instances`, `config/`,
`logs/`, `rec/`).

```json
{"bind": "127.0.0.1", "port": 7320, "join_code": "XXXXXX"}
```

`tls_cert` and `tls_key` add TLS. Bound to anything but loopback without TLS,
the server warns: reach it only over a network you trust.

## Connecting

Offer the subprotocol `sparkgap.server.v1`, and the join code as
`sparkgap.code.<code>` (a browser) or `Authorization: Bearer <code>`. The same
code opens `/feed/<name>`. A wrong code closes with 4401; five wrong codes from
one address lock it out for 10 minutes (close 4429).

## Messages from the server

`hello`, once:

```json
{"type": "hello", "v": 1, "server": true, "host": "rednuc", "build": "3f41c0b"}
```

`seat`, on connect and every 2 seconds:

```json
{"type": "seat", "utc": 1792690000.0,
 "instances": [{"name": "Dallas-1", "state": "running", "busy": null,
                "pid": 2014134, "band": "17m", "radio": "Dallas",
                "feed_port": 7321, "feed_web": true, "telnet_port": 7331,
                "settings": {"band": "17m", "bands_known": ["160m", "80m"],
                             "validation": "normal", "call_source": "rbn+skcc",
                             "spot_rule_contest_words": false,
                             "spot_rule_cq_repeats": false, "record": false},
                "spots": 12, "top_calls": [["K8TLJ", 3]], "errors": 0,
                "last_error": null, "warnings": 2, "reconnects": 0,
                "log": "Dallas-1_17m_20261022_152327Z.log",
                "started": 1792690000.0}],
 "stations": [{"radio": "Dallas", "state": "running",
               "station": "SparkGap-Dallas", "live": "live", "rtt_ms": 2.8,
               "losses_hour": 0, "last_loss": null}]}
```

- `state`: `running` or `stopped`. `busy`: the operation in progress
  (`starting`, `stopping`, `restarting`, `adding`, `removing`, `changing band`,
  `changing the receive path`, `changing its rbn feeder`, `saving the node`,
  `discovering`), else null.
- `feed_port`: the port this run of the copy got, null while stopped.
- `last_error`: the last error in a stopped copy's log.
- `stations`: the Flex station of each radio the instances use. `state` is the
  process, `running` or `stopped`. The rest is what the station reports (on
  each heartbeat and as a recovery starts), absent until its first report:
  `live` is the radio connection (`connecting`, `live`, `reconnecting`,
  `halted`, `stopped`), then `station`, `rtt_ms`, `losses_hour` and `last_loss`.

The message also carries, one line each:

- `radios`: each radio file's node (`name`, `type`, `model`, `address`,
  `callsign`, `grid`, `op_name`, `rbn_upload`, `rx_settings`) and `feeder`,
  the status its RBN feeder last sent, null when none is linked.
- `radio_files`: every radio file in `config/`, for setup.
- `bands`: the bands an added receiver can run.
- `default_settings`: the settings an added receiver starts with.
- `removed`: the settings of receivers taken out, which `add` restores.
- `processes`: every linked process (see the process hub).

`log`, on connect the server's last 200 log lines (`"reset": true`), then each
new line as it is logged: connections and disconnections, requests, who made
them and their results, refusals, the warnings and errors of each linked process
(`Station Dallas: ...`), and a radio's connection changes (`Server: radio Dallas
connected`, `connecting`, `reconnecting`, `not connected (halted)` or `not
connected`).

```json
{"type": "log", "reset": true,
 "lines": [{"utc": 1792690000.123, "level": "INFO",
            "msg": "Server: restart Dallas-1 by SparkGap Web (100.64.0.2)"}]}
```

`server_result`, for each request:

```json
{"type": "server_result", "id": 7, "ok": true, "message": "Dallas-1: live (pid 2014134)"}
{"type": "server_result", "id": 8, "ok": false,
 "error": "callsign cannot be changed here"}
```

## Messages to the server

```json
{"type": "server", "id": 7, "cmd": "start", "name": "Dallas-1"}
{"type": "server", "id": 8, "cmd": "set", "name": "Dallas-1",
 "settings": {"validation": "normal", "call_source": "rbn+skcc"}}
```

- `discover` (no `name`): listen for the FlexRadios on the local network and
  write each one's radio file, `config/<nickname>.radio.json` (nickname,
  model, serial, address); `message` lists what was found and changed. An
  optional `settings.address` asks that one address instead. A Flex
  instance names its radio with `radio`.
- `node` (no `name`, `settings`: `radio`, `callsign`, `grid`, `op_name`):
  writes that radio file's spotter ID, grid and operator.
- `by`, optional on any request: the client's own name (`SparkGap Web`), put in
  the log with its address.
- `cmd`: `start`, `stop`, `restart`, `set`, `add`, `remove`, `discover` or
  `node`. A Flex instance's radio station is started before its first copy
  and stopped after its last.
- `set` writes the settings to the instance's config and restarts it if it is
  running. Only these, each checked: `band` (a band the instance's SparkGap
  knows), `validation` (`off`, `permissive`, `normal`, `aggressive`),
  `call_source` (`scp`, `rbn`, `rbn+skcc`), `afc` (true or false, written as
  `itila2_afc`), `record` (true or false; recording on needs 5 GB free in
  `rec/`), the on/off decoder options
  `spot_rule_contest_words`, `spot_rule_cq_repeats`, `spot_rule_doubled_calls`,
  `spot_rule_split_up`, `spot_rule_owner_cell`, `spot_rule_owned_two_windows`, `spot_rule_clip_busts`,
  `spot_rule_la1d`, `spot_rule_gluek`,
  `spot_rule_norival` and `itila2_snr_decoder`, and `spot_rule_fragments`
  (`any`, `any,stray`, `even`, `even,stray`) and `spot_rule_strongest_db`
  (3, 6, 10), where null is off
  and removes the key, and `itila_window_sec` (60, 30, 15, 7.5). Anything
  else is refused and nothing is written.
- A running copy given the band alone is changed live: the server writes the
  band to the config (the startup band), then asks the copy over its feed (a
  `control` message, `GUI_FEED_PROTOCOL.md`, waiting up to 20 s) to change band
  in place. On success the result reads `Dallas-1: retuned to 20m` and the copy
  keeps running (same process, uptime continues). If the copy refuses, does not
  answer or has no feed, the server restarts it, which brings it up on the band
  just written, and the result reads as for a restart. Any other mix of settings
  is applied by a restart, and a stopped copy is only written.
- A Flex instance's receive path, `antenna` (one the radio lists), `loop_a`,
  `loop_b` (true or false) and `rf_gain` (whole dB), given to a running copy
  goes to the radio only; given to a stopped copy, it is written as its startup
  values (`flex_rxant`, `flex_loopa`, `flex_loopb`, `flex_rfgain`). A key left
  out starts with what the radio has.
- SparkGap follows the radio: a running copy takes whatever changes its receive
  path (SparkGap Web, SmartSDR, SmartControl, the front panel), and a reconnect
  restores it. SparkGap never writes the startup values itself.
- `add` makes a Flex receiver `<radio>-<n>` on DAX-IQ channel n, its telnet port
  7330 plus n and UDP port 7790 plus n, or the next free on the machine
  (`settings`: `band`, and any setting `set` takes): its config from the shipped
  defaults (`skimmer_flex.example.json` in the server's code), or the one it had
  before it was removed, with the radio file's spotter ID and grid and no
  antenna or RF gain; its instances line runs the server's own code. Then it
  starts. Refused when the radio is full or the channel is in use.
- `remove` stops a receiver and takes it out of `instances`; its config, logs
  and recordings stay.
- One request at a time per instance: another while one runs is refused as
  busy. Requests run in order, at most 8 waiting. The instance's lock is held
  through the write, the live attempt and any restart.

Each start, stop and settings change takes the radio's lock, then the
instance's (`logs/station_<radio>.lock`, `logs/<name>.lock`), so a script of
your own can take the same locks and run beside the server.

## The process hub

The processes the server runs on its machine (a copy, its radio's station,
an RBN feeder) link to it at `/process`, subprotocol `sparkgap.process.v1`:
loopback only, no join code. A link is the server's view of the process:
linked means running, a closed link means it stopped. A process keeps
running when the server is away and links again when it is back
(`process_link.py`).

- `{"type": "hello", "role": "feeder", "name": "Dallas", "pid": 1234,
  "pgid": 1234}`: first, from the process. `role` is `skimmer`, `station`
  or `feeder`; `name` is the instance, or the radio for a station or a
  feeder. A second
  link under the same role and name replaces the first.
- `{"type": "status", ...}`: from the process, its status fields whenever
  they change. Each `status` replaces the whole status. The message type is
  set last, so a status field named `type` never hides it, and the server
  drops `type`. The server keeps the latest and lists every linked process
  in `seat` messages under `processes`.
- `{"type": "log", "level": "WARNING", "msg": "..."}`: from the process for each
  log line at WARNING or worse, cut at 400 characters and dropped while the
  server is away. The server logs it as `<Role> <name>: <msg>`.
- `{"type": "command", "id": "...", "cmd": "stop", "args": {}}`: from the
  server; the process answers `{"type": "result", "id": "...", "ok": true,
  "message": "..."}` (or `"error"` when `ok` is false).

A skimmer (a live `sparkgap.py`) sends `state`, `feed_port`, `telnet_port`,
`spots`, `cpu`, `rss_mb` and `receivers` (each `band`, `centre_khz`, `rx`) and
takes `stop` and `note` (text for its log: who asked for what is about to
happen). An instance whose skimmer is linked shows `linked: true`, `state:
running` and the link's `feed_port` and `receivers`. A station's link status is
its health in `stations`.

A radio's RBN feeder is its node: it links as `feeder <radio>`, takes its
spotter ID, grid, operator and `rbn_upload` from `config/<radio>.radio.json`
and reads every running receiver of the radio. The server starts it with the
radio's first receiver, sends `receivers` (`{"names": [...]}`) as receivers
start and stop, and stops it after the last. `set` of `rbn_upload` on any
receiver sets it for the radio. A receiver without a radio file has its own
feeder, named for it.
