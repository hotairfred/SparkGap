# How to send spots to RBN

For SparkGap operators testing live uploads. SparkGap's feeder,
`rbn2_feeder.py`, reads the spots from SparkGap's local telnet server and posts
them to RBN the way Aggregator does, so a SparkGap node needs no Windows
machine.

## What you need

- SparkGap running, with its telnet server on (`telnet_port` in your config,
  default 7300).
- A SparkGap checkout that has `rbn2_feeder.py`. Linux x86_64 and python3.
- **The RBN auth library, `librbn2_auth.so`.** The feeder depends on it: RBN
  accepts uploads only from a feeder that identifies itself the way RBN expects,
  and the library does that part. It is not in SparkGap's repo and is not
  published. It is available from Chris WX7V on request, as a download link with
  a checksum file.
- Your callsign and grid in the SparkGap config.

## Setup

1. **Get the library** from Chris WX7V, with `SHA256SUMS.txt` from the same
   link. In the folder holding both, check it: `sha256sum -c SHA256SUMS.txt`
   should print `librbn2_auth.so: OK`.
2. **Put it in the folder that contains your SparkGap checkout,** not inside the
   checkout. From inside the checkout: `mv /path/to/librbn2_auth.so ..`. With
   the checkout at `~/sparkgap/code`, the file ends up at
   `~/sparkgap/librbn2_auth.so`. Keep the name `librbn2_auth.so`. It stays out
   of the checkout so git never picks it up.
3. **Set the config.** In your SparkGap config:

   <!-- markdownlint-disable MD013 -->
   | Key | Value |
   | --- | --- |
   | `callsign`, `grid` | Required. The spotter RBN lists is `callsign` plus `skimmer_suffix`, except the default `-#` |
   | `op_name`, `qth` | Optional; RBN's node list shows them |
   | `spot_validation` | `permissive`, `normal` or `aggressive`, or absent for off; sent to RBN as the node's validation level |
   | `rbn_upload` | `"dry"` reads spots and logs what it would post, posting nothing; `"live"` posts; absent is off |
   <!-- markdownlint-enable MD013 -->

4. **Dry run first:** `python3 rbn2_feeder.py --config <your config>` with
   `"rbn_upload": "dry"`, or add `--dry-run` to any config. It logs `[DRY] POST`
   lines for what it would send. Check that the spots look right and the bands
   match what you are skimming.
5. **Go live:** set `"rbn_upload": "live"` and run the same command. The
   start-up lines show `auth library rbn2auth/1.2 from <path>` and `... LIVE`.
6. **Check RBN:** within a few minutes your node shows in RBN's node list, and
   your spots show on reversebeacon.net and its telnet feed under your spotter
   call.

Stop the feeder with Ctrl+C (or SIGTERM).

## What it sends

- An ID message at start and every 300 s: your call, grid, name, QTH, validation
  level, the bands your skimmer reports, and SparkGap's version. Spots wait
  until the first one succeeds.
- Your spots, CW and RTTY, in one post per upload tick when there are any;
  nothing is sent when there are none. A failed post is not retried: its spots
  are dropped.

Only send spots once your node's spots hold up against other skimmers on the
same signals. Busted calls and callers spotted as CQ end up on RBN under your
call.

## When something is wrong

<!-- markdownlint-disable MD013 -->
| Message | Meaning |
| --- | --- |
| `not posting: no <path>: RBN would drop these spots` | The library is not where the feeder looks; `<path>` is the exact place to put it |
| `<path> is version '...', this feeder takes rbn2auth/1.2` | The library and the feeder don't match; ask Chris WX7V for the current library, or update your checkout |
| `cannot load <path>` | The file is damaged or not for this machine; download it again and check the sum |
| `rbn_upload is None: off` | The config has no `rbn_upload` key |
| `callsign and grid must both be set` | Fill both in the config |
| `no band info from the skimmer's SETT: empty bandLimits` | SparkGap's telnet server did not report its bands; check it is running and on the configured port |
| `id.php failed` or `s.php failed` | RBN could not be reached or refused the post; the ID message retries after 60 s |
<!-- markdownlint-enable MD013 -->

Keep the library and its link to yourself: they are for testers, not for
redistribution.

## With SparkGap Web

With the SparkGap server ([SERVER_SETUP.md](SERVER_SETUP.md)), the server runs
one feeder per radio, fed by all its receivers; there is nothing to start by
hand.

- **Spotter ID, grid and operator:** Setup in SparkGap Web's Server pane, or
  `sparkgap.sh setup`. They are saved in the radio file,
  `config/<radio>.radio.json`, not in each receiver's config.
- **RBN upload:** off, dry or live in the Server pane, for the whole radio. Only
  the feeder restarts.
- **The library:** in the run folder, beside `code/`:
  `~/sparkgap/librbn2_auth.so`.
- **Telnet:** each receiver's port is set by Add (7330 plus its DAX-IQ channel);
  the feeder reads them itself.
- **Messages:** the feeder's warnings and errors, including those above, show in
  the server's log; its own log is `logs/rbn2_<radio>_<UTC>.log`.
