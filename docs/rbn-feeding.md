# Feeding RBN from sparkgap

sparkgap's CW (and FT8/PSKReporter) spots can be relayed directly to
the Reverse Beacon Network without going through Aggregator on a
Windows box.  This document describes how.

## Architecture

```
   ┌─────────────────┐         ┌───────────────────┐         ┌─────────────┐
   │   sparkgap.py   │  :7300  │   rbn_feeder.py   │  HTTPS  │     RBN     │
   │ (decoder + CW)  │ telnet  │  (spot forwarder) │         │  ingestion  │
   └─────────────────┘ ──────▶ └───────────────────┘ ──────▶ └─────────────┘
```

sparkgap emits DX-de spot lines on its local telnet cluster (port
`telnet_port` from your config, default `7300`).  `rbn_feeder.py`
connects to that telnet, parses each spot, and posts it to RBN's
ingest endpoints.  Both processes can run on the same host or
different hosts on your LAN.

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

## Running rbn_feeder.py

Basic invocation:

```
python3 rbn_feeder.py \
    --call WF8Z \
    --grid EM79SM \
    --local-host 127.0.0.1 \
    --local-port 7300
```

- `--call`: your amateur callsign.  Optional `-#` skimmer suffix
  permitted (e.g. `WF8Z-1`); must be numeric if present.  See
  `python3 rbn_feeder.py --help` for the suffix-format guard.
- `--grid`: your 4–6 char Maidenhead locator.
- `--local-host` / `--local-port`: where sparkgap's cluster telnet
  is listening.  Defaults are sane for same-host setups.

The feeder reconnects on either side if connections drop, so it's
safe to run as a long-lived background daemon (systemd, `nohup`,
tmux, whatever you prefer).

## Health monitoring

`rbn_feeder.py` logs at INFO when:
- registration with RBN succeeds (`id.php` 200 OK)
- spot batches upload (`s.php` 200 OK with batch size)
- the local telnet feed disconnects/reconnects

Watch for repeated `s.php` non-200 responses — that usually means
your authentication is wrong, not an RBN outage.

## What gets relayed

Currently `rbn_feeder.py` relays **CW and RTTY** spots only.  FT8
spots use a separate path direct to PSKReporter (handled inside
sparkgap.py itself).  This split mirrors what Aggregator does — RBN
doesn't accept FT8 spots through its ingest endpoint.

## Common operational notes

- **Don't route rbn_feeder through GoCluster.**  Feed rbn_feeder
  from sparkgap's own telnet, not from a downstream cluster
  aggregator.  Routing through a consensus filter strips a fair
  number of legitimate weak/local QSOs and degenerately
  self-validates spots the feeder already saw.

- **Multi-host setups are supported but not yet documented in
  detail.**  If you want sparkgap on one box and rbn_feeder on
  another, set `--local-host` to the sparkgap box's LAN IP and
  ensure firewall rules allow the connection.  Pre-1.0 we treat
  this as bespoke — the in-tree config assumes single-host.

- **Stopping cleanly.**  rbn_feeder.py handles SIGTERM but sometimes
  takes a few seconds to flush its outgoing queue.  Give it 10s
  before SIGKILL.

## Reverse-engineering details

The wire protocol between rbn_feeder.py and RBN is intentionally
documented only in the source of `rbn_feeder.py` itself — read the
file's module docstring and the `_post_id` / `_post_spots` helpers
if you need to understand it.  We're not separately publishing the
protocol writeup, the Wireshark analysis, or hash-algorithm details.
If you need that level of insight you can derive it from the source;
this is a "show your work in the code, don't compile a separate
how-to-replicate-Aggregator guide" decision.

## Cross-reference

- `feedback_rbn_raw_upstream.md` (internal memory) — explains the
  RAW vs validated-spots role split between skimmer and consumer
- `project_pre_live_rbn_checklist.md` (internal memory) — pre-live
  gotchas (suffix-must-be-numeric, etc) we hit on our own first
  rollout
- `feedback_rbn_ops_lessons.md` (internal memory) — industry context
  from the RBN-OPS mailing list
