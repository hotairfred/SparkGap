# Feeding RBN from sparkgap

sparkgap's CW (and FT8/PSKReporter) spots can be relayed directly to
the Reverse Beacon Network without going through Aggregator on a
Windows box.  This document describes how.


> **Beta.** Beta testers are running live uploads with `rbn2_feeder.py`.
> Everyone else keeps `rbn_upload` off until their node's spots hold up
> against other skimmers on the same signals (hotairfred/SparkGap#49).

The feeder for live uploads is `rbn2_feeder.py`. It depends on the RBN auth
library, `librbn2_auth.so`, which is not in this repo and is available from
Chris WX7V on request. How to set it up and go live:
[sending-spots.md](sending-spots.md). The library's interface and how the feeder
uses it: [rbn-auth-library.md](rbn-auth-library.md).

## Architecture

```
   ┌─────────────────┐         ┌───────────────────┐         ┌─────────────┐
   │   sparkgap.py   │  :7300  │  rbn2_feeder.py   │  HTTPS  │     RBN     │
   │ (decoder + CW)  │ telnet  │  (spot forwarder) │         │  ingestion  │
   └─────────────────┘ ──────▶ └───────────────────┘ ──────▶ └─────────────┘
```

sparkgap emits DX-de spot lines on its local telnet cluster (port
`telnet_port` from your config, default `7300`).  `rbn2_feeder.py`
connects to that telnet, parses each spot, and posts it to RBN's
ingest endpoints.

## Prerequisites

1. **A valid amateur callsign you control.**  RBN identifies feeds by
   callsign; using someone else's is a fast track to being banned
   from the network.

2. **Coordinate with RBN-OPS before going live in production.**  The
   RBN team typically wants a heads-up if you're spinning up a new
   feeder, especially one that's not the standard Aggregator client.
   They've been welcoming of native-Linux feeders in our experience.

3. **A quality bar.**  Don't point a feeder at a misconfigured
   skimmer.  Bad spots get the operator (you) banned; not great for
   the project either.  Validate locally first — spot-check your
   spots against a known cluster for a few days before going live.

## Running rbn2_feeder.py

Without the server, run one feeder per SparkGap copy:

```
python3 rbn2_feeder.py --config config/<seat>.local.json [--dry-run]
```

It takes the callsign, grid, `op_name`, `qth`, `spot_validation`, `telnet_port`
and `rbn_upload` from that config ([sending-spots.md](sending-spots.md) has the
table). Stop it with Ctrl+C or SIGTERM.

## With the SparkGap server

With the server (`sparkgap_server.py`), each radio has one `rbn2_feeder.py`, run
as `rbn2_feeder.py --radio config/<radio>.radio.json --receiver <name> ...`. The
server starts it with the radio's first receiver and stops it after the last,
and tells it as receivers start and stop. It takes the spotter ID, grid,
operator and `rbn_upload` (`off`, `dry` or `live`) from the radio file, set in
SparkGap Web under Server (Setup, RBN upload), and posts a call copied by two
receivers once per re-spot interval. Its messages are in
[SERVER_PROTOCOL.md](SERVER_PROTOCOL.md).

## rbn_feeder.py

The first feeder, `rbn_feeder.py` (`--call`, `--grid`, `--local-host`,
`--local-port`), is superseded for uploads. It stays for the spot parsing and
band probe `rbn2_feeder.py` reuses.

## Health monitoring

`rbn2_feeder.py` logs its start-up (the auth library's version and path, and dry
run or LIVE), each failed `id.php` or `s.php` post, and the local telnet feed
disconnecting and reconnecting. What each warning means:
[sending-spots.md](sending-spots.md#when-something-is-wrong). With the server,
its warnings and errors also show in the server's log.

## What gets relayed

`rbn2_feeder.py` relays **CW and RTTY** spots only.  FT8
spots use a separate path direct to PSKReporter (handled inside
sparkgap.py itself).  This split mirrors what Aggregator does — RBN
doesn't accept FT8 spots through its ingest endpoint.

## Common operational notes

- **Don't route the feeder through GoCluster.**  Feed it
  from sparkgap's own telnet, not from a downstream cluster
  aggregator.  Routing through a consensus filter strips a fair
  number of legitimate weak/local QSOs and degenerately
  self-validates spots the feeder already saw.

- **Stopping cleanly.**  The feeder handles SIGTERM.  A post that
  fails is not retried: its spots are dropped.

## Reverse-engineering details

The wire protocol between the feeders and RBN is intentionally
documented only in the source of `rbn_feeder.py` and `rbn2_feeder.py`:
read their module docstrings if you need to understand it.  The two
fields RBN checks come from the private auth library; no doc gives its
hash method, sign-in details or hash values, and the feeder never logs
them.

## Cross-reference

- `feedback_rbn_raw_upstream.md` (internal memory) — explains the
  RAW vs validated-spots role split between skimmer and consumer
- `project_pre_live_rbn_checklist.md` (internal memory) — pre-live
  gotchas (suffix-must-be-numeric, etc) we hit on our own first
  rollout
- `feedback_rbn_ops_lessons.md` (internal memory) — industry context
  from the RBN-OPS mailing list
