#!/usr/bin/env python3
"""RBN spot feeder — bridges OpenSkimmer's local telnet (:7300) to the
Reverse Beacon Network's central spot ingest.

ARCHITECTURE
============

OpenSkimmer  --[DX de format]-->  localhost:7300  (already produces this)
                                        |
                                        | (this script)
                                        v
                                 [RBN ingest endpoint]
                                 host:port + login
                                 (TBD — capture from Aggregator)

This is the open-source replacement for VE3NEA's Aggregator.  Aggregator
takes the same input (Skimmer/SkimSrv telnet) and forwards to RBN's
central server.  We do the same thing in 100 lines of Python on Linux.

UNKNOWNS
========

Filling these in once Aggregator's network behavior is captured under
Wine + Wireshark/tcpdump:

  RBN_HOST     — DNS name of the RBN spot-ingest server
  RBN_PORT     — TCP port (probably 7000 or 7300)
  LOGIN_FORMAT — what bytes Aggregator sends to authenticate
  SPOT_FORMAT  — does it forward the prefixed "DX de CALL-#: ..." line
                 verbatim, or use a "DX FREQ CALL COMMENT" command?

Sensible guess (standard DX cluster behavior):
  - host: feed.reversebeacon.net OR submit.reversebeacon.net
  - port: 7000
  - login: just "<CALL>\\n" then read the banner
  - format: forward the prefixed "DX de CALL-#: ..." line verbatim,
    or send "DX FREQ CALL COMMENT" if the server prefers commands

USAGE
=====

  python3 rbn_feeder.py --call WF8Z-# --rbn-host feed.reversebeacon.net \\
                        --rbn-port 7000

Run as a daemon alongside openskimmer.py.  Reads local telnet from
127.0.0.1:7300; reconnects to either end on disconnect.

OPERATIONAL HYGIENE
===================

- Local blacklist applied before forwarding (--blacklist blacklist.txt)
- Logs every forwarded spot with a wallclock timestamp to stdout
- Logs rejections (blacklisted, malformed, etc.) at DEBUG
- Never crashes on bad input — log and continue

This is the gating piece between "OpenSkimmer is a local tool" and
"OpenSkimmer is an RBN node."  Until this works, our spots stay local.
"""

import argparse
import logging
import re
import socket
import sys
import time
from datetime import datetime, timezone

log = logging.getLogger('rbn_feeder')

# Local skimmer telnet (matches openskimmer.py's telnet_port default).
# Override with --local-host / --local-port if running the feeder on a
# different machine than openskimmer.py.
LOCAL_HOST = '127.0.0.1'
LOCAL_PORT = 7300

# Standard DX-cluster spot line format produced by openskimmer.py.
# Example: "DX de WF8Z-#:    14025.50  R2HE         CW   15 dB  26 WPM  CQ  OS  1313Z"
# Mode column (CW/FT8/RTTY/...) lives between dx_call and dB.
SPOT_RE = re.compile(
    r'^DX de (\S+):\s+(\d+\.\d+)\s+(\S+)\s+(\S+)\s+(.*?)(\d{4}Z)?\s*$'
)

# Modes RBN accepts. RBN is a CW-and-RTTY network — FT8/FT4/digital spots
# go to PSKReporter via a different protocol (not implemented here).
RBN_MODES = {'CW', 'RTTY'}


def stamp():
    return datetime.now(timezone.utc).strftime('%H:%M:%S')


def load_blacklist(path):
    if not path:
        return set()
    try:
        with open(path) as f:
            return {line.strip().upper() for line in f
                    if line.strip() and not line.startswith('#')}
    except FileNotFoundError:
        log.warning("blacklist not found: %s (continuing without)", path)
        return set()


def connect_local(host=LOCAL_HOST, port=LOCAL_PORT):
    """Connect to OpenSkimmer's local telnet (default :7300) and return a socket."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(60)
    s.connect((host, port))
    # Wait for prompt, send our call (openskimmer's telnet expects a login).
    time.sleep(0.5)
    try:
        banner = s.recv(8192)
        log.debug("local banner: %r", banner[:200])
    except socket.timeout:
        pass
    s.sendall(b'WF8Z\n')  # placeholder — openskimmer accepts any call
    return s


def connect_rbn(host, port, call):
    """Connect to the RBN spot-ingest endpoint and authenticate.

    The exact handshake is TBD — this is the placeholder shape based on
    standard DX-cluster behavior.  Capture Aggregator under Wine to
    confirm: hostname/port, login byte sequence, spot wire format.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(60)
    s.connect((host, port))
    time.sleep(0.5)
    try:
        banner = s.recv(8192)
        log.info("RBN banner: %r", banner[:200])
    except socket.timeout:
        pass
    # Standard DX-cluster login — TBD: confirm format Aggregator uses.
    s.sendall(f'{call}\n'.encode('ascii'))
    log.info("Logged in to RBN as %s", call)
    return s


def parse_spot(line):
    """Parse an openskimmer telnet 'DX de ...' line into structured fields.
    Returns dict with freq, call, mode, body, time or None if not a spot."""
    m = SPOT_RE.match(line.strip())
    if not m:
        return None
    return {
        'spotter': m.group(1),
        'freq':    float(m.group(2)),
        'call':    m.group(3).upper(),
        'mode':    m.group(4).upper(),
        'body':    m.group(5).strip(),
        'time':    m.group(6) or '',
    }


def forward_loop(local_sock, rbn_sock, blacklist, dry_run=False):
    """Read lines from local, parse, forward to RBN.  Blocks until either
    side disconnects.  Caller handles reconnect."""
    buf = b''
    counts = {'forwarded': 0, 'blacklisted': 0, 'malformed': 0, 'non_spot': 0,
              'wrong_mode': 0}
    last_status = time.time()
    while True:
        chunk = local_sock.recv(8192)
        if not chunk:
            log.warning("local telnet disconnected (EOF)")
            return counts
        buf += chunk
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            text = line.decode('ascii', errors='replace').rstrip('\r')
            if not text or 'DX de' not in text:
                counts['non_spot'] += 1
                continue
            spot = parse_spot(text)
            if not spot:
                counts['malformed'] += 1
                log.debug("malformed: %r", text)
                continue
            if spot['mode'] not in RBN_MODES:
                # FT8/FT4/digital — wrong destination, drop quietly.
                # PSKReporter would be the right home (separate feeder).
                counts['wrong_mode'] += 1
                log.debug("wrong mode for RBN: %s @ %.1f (%s)",
                          spot['call'], spot['freq'], spot['mode'])
                continue
            if spot['call'] in blacklist:
                counts['blacklisted'] += 1
                log.debug("blacklisted: %s @ %.1f", spot['call'], spot['freq'])
                continue
            # Forward — verbatim line is the safest, most-cluster-compatible form
            wire = (text + '\n').encode('ascii', errors='replace')
            if dry_run:
                log.info("[DRY] %s %s @ %.1f kHz", spot['mode'], spot['call'], spot['freq'])
            else:
                try:
                    rbn_sock.sendall(wire)
                except (BrokenPipeError, ConnectionResetError) as e:
                    log.warning("RBN socket dead (%s) — reconnecting", e)
                    return counts
                log.info("→ %s %s @ %.1f kHz [%s]", spot['mode'], spot['call'],
                         spot['freq'], spot['body'][:40])
            counts['forwarded'] += 1
        # Periodic status line
        now = time.time()
        if now - last_status > 60:
            log.info("status: %s", counts)
            last_status = now


def main():
    p = argparse.ArgumentParser(description='RBN spot feeder for OpenSkimmer')
    p.add_argument('--call', required=True, help='Skimmer callsign for RBN login (e.g. WF8Z-#)')
    p.add_argument('--local-host', default=LOCAL_HOST,
                   help='OpenSkimmer telnet hostname (default 127.0.0.1)')
    p.add_argument('--local-port', type=int, default=LOCAL_PORT,
                   help='OpenSkimmer telnet port (default 7300)')
    p.add_argument('--rbn-host', default='feed.reversebeacon.net',
                   help='RBN ingest hostname (TBD — confirm via Aggregator capture)')
    p.add_argument('--rbn-port', type=int, default=7000,
                   help='RBN ingest port (TBD)')
    p.add_argument('--blacklist', default='blacklist.txt',
                   help='Path to local blacklist (one CALL per line)')
    p.add_argument('--dry-run', action='store_true',
                   help='Parse local spots and log them but do not actually forward upstream')
    p.add_argument('--log-level', default='INFO',
                   choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )

    blacklist = load_blacklist(args.blacklist)
    log.info("loaded %d blacklist entries", len(blacklist))

    while True:
        local_sock = None
        rbn_sock = None
        try:
            local_sock = connect_local(args.local_host, args.local_port)
            log.info("connected to local %s:%d", args.local_host, args.local_port)
            if not args.dry_run:
                rbn_sock = connect_rbn(args.rbn_host, args.rbn_port, args.call)
                log.info("connected to RBN %s:%d", args.rbn_host, args.rbn_port)
            counts = forward_loop(local_sock, rbn_sock, blacklist, args.dry_run)
            log.info("session ended: %s", counts)
        except KeyboardInterrupt:
            log.info("interrupted, exiting")
            sys.exit(0)
        except Exception as e:
            log.warning("error: %s — reconnecting in 5s", e)
        finally:
            for s in (local_sock, rbn_sock):
                if s:
                    try: s.close()
                    except Exception: pass
        time.sleep(5)


if __name__ == '__main__':
    main()
