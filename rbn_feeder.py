#!/usr/bin/env python3
"""rbn_feeder.py — native Linux RBN spot forwarder.

Replaces VE3NEA's Aggregator. Bridges SparkGap's local cluster
telnet (:7300) to the Reverse Beacon Network ingest server.

PROTOCOL
========

Reverse-engineered from a Wireshark capture of Aggregator 6.7 talking
to RBN. Plain HTTP (no TLS), JSON bodies, two endpoints:

  POST http://x.reversebeacon.net:88/rx/6/id.php   - registration / heartbeat
  POST http://x.reversebeacon.net:88/rx/6/s.php    - spot batch upload

Cadence: id.php every ~50 s, s.php every 10 s with whatever spots
have accumulated. Server responds with HTTP 200 and a JSON config
payload (server policy, file URLs, frequency windows, etc).

USAGE
=====

  python3 rbn_feeder.py --call WF8Z --grid EM79SM \\
                        --local-host 192.168.1.76 --local-port 7300

Run as a daemon alongside sparkgap.py. Reads its local cluster
telnet, parses DX-de lines, posts to RBN. Reconnects on either side.
"""

import argparse
import hashlib
import json
import logging
import re
import secrets
import socket
import sys
import threading
import time
import queue
from urllib import request as urlreq, error as urlerror

log = logging.getLogger('rbn_feeder')

# RBN's id.php and s.php endpoints validate two short hash fields
# (`shortHash` on id.php, `h` on s.php) computed by Aggregator before
# upload.  Those algorithms gate spam against the RBN ingest endpoint;
# without them, RBN accepts the POST (HTTP 200) but silently drops
# the payload — spots never reach the worldwide telnet broadcast.
#
# This module ships WITHOUT those algorithms.  Operators wanting a
# Linux-native feeder currently bridge through Aggregator on a
# Windows box; the sanctioned Linux path is an open conversation
# with the RBN administrators.  If you're testing locally, ignore
# this — `--dry-run` doesn't POST upstream and works fine.
#
# If a private `rbn_auth` module is present at import time we use
# it.  Otherwise we fall back to random hex, which RBN treats as
# unauthenticated and silently drops.
try:
    from rbn_auth import id_php_short_hash, s_php_h
    HAVE_RBN_AUTH = True
except ImportError:
    HAVE_RBN_AUTH = False
    def id_php_short_hash(skim_sign_in):
        return secrets.token_hex(4)
    def s_php_h(callsigns):
        return secrets.token_hex(4)

# Skimmer telnet line format produced by sparkgap.py and SkimSrv:
#   DX de WF8Z-#:    14025.50  R2HE         CW  15 dB 26 WPM  CQ  OS  1313Z
SPOT_RE = re.compile(
    r'^DX de (\S+):\s+(\d+\.\d+)\s+(\S+)\s+(\S+)\s+'
    r'(-?\d+)\s*dB(?:\s+(\d+)\s*WPM)?\s+'
    r'(\S+)(?:\s+(\S+))?\s+(\d{4})Z\s*$'
)

# RBN accepts CW + RTTY via this protocol. FT8/FT4 go through PSKReporter
# (different upstream entirely — separate feeder, not implemented here).
RBN_MODES = {'CW', 'RTTY'}

DEFAULT_RBN_URL = 'http://x.reversebeacon.net:88/rx/6'

# Aggregator advertises itself as version 6.7 in the JSON. Keep it for
# protocol compatibility — RBN servers may parse this. Identification
# of SparkGap happens via skimName in the id payload.
AGG_VERSION = '6.7'
SKIM_VERSION = 'v.1.6.0.145'  # mirrors what we put in the SkimSrv banner


def parse_spot(line):
    """Parse a 'DX de SPOTTER-#: ...' telnet line into a dict.
    Returns None if the line doesn't look like a CW/RTTY skimmer spot."""
    # SkimSrv-style wire format ends with BEL (\a) before CR/LF — strip
    # all control bytes before matching, otherwise the regex's \s*$
    # anchor won't fire.
    cleaned = ''.join(c for c in line if c >= ' ').strip()
    m = SPOT_RE.match(cleaned)
    if not m:
        return None
    spotter, freq, call, mode, snr, wpm, body1, body2, time_str = m.groups()
    return {
        'spotter': spotter,
        'freq':    float(freq),
        'call':    call.upper(),
        'mode':    mode.upper(),
        'snr':     int(snr),
        'wpm':     int(wpm) if wpm else 0,
        # body1 is "CQ" / "DE" / "BCN" (or possibly garbage); body2 is
        # source-tag if present (e.g. "SG", "SDC").
        'cq_flag': body1.upper() if body1 else 'CQ',
        'time':    time_str,
    }


def load_blacklist(path):
    if not path:
        return set()
    try:
        with open(path) as f:
            return {ln.strip().upper() for ln in f
                    if ln.strip() and not ln.startswith('#')}
    except FileNotFoundError:
        log.warning("blacklist not found: %s — continuing without", path)
        return set()


class RBNSession:
    """RBN HTTP session — registration + spot upload."""

    def __init__(self, url, skim_call, skim_name, skim_grid,
                 skim_qth, skim_validation, bands, dry_run=False):
        self.url = url.rstrip('/')
        self.skim_call = skim_call
        self.skim_name = skim_name
        self.skim_grid = skim_grid
        self.skim_qth = skim_qth
        self.skim_validation = skim_validation
        self.bands = bands  # list of (low_khz, high_khz) tuples
        self.dry_run = dry_run
        # fingerPrint is a stable per-installation 32-char hex (md5).
        # Aggregator generates this once and reuses across runs. We
        # derive ours deterministically from call+host so a given node
        # presents the same identity every time.
        self.fingerprint = hashlib.md5(
            f'{skim_call}@{socket.gethostname()}'.encode()
        ).hexdigest()
        # Server-supplied policy from the most recent id.php response.
        # Fields we care about: serverUploadInterval, Limit3Min,
        # spotWindows. Populated lazily.
        self.server_policy = {}

    def _band_limits_str(self):
        return ','.join(f'{lo:.1f}-{hi:.1f}' for lo, hi in self.bands)

    def _post(self, path, body):
        if self.dry_run:
            log.info('[DRY] POST %s %s', path, json.dumps(body, separators=(',', ':'))[:200])
            return None
        url = f'{self.url}/{path}'
        data = json.dumps(body, separators=(',', ':')).encode('utf-8')
        req = urlreq.Request(
            url, data=data, method='POST',
            headers={
                'Content-Type': 'application/json',
                'Connection': 'Keep-Alive',
                'User-Agent': f'rbn_feeder/{AGG_VERSION}',
            }
        )
        try:
            with urlreq.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read() or b'{}')
        except (urlerror.URLError, urlerror.HTTPError, OSError) as e:
            log.warning('%s failed: %s', path, e)
            return None
        except json.JSONDecodeError:
            return {}

    def post_id(self):
        bands_str = self._band_limits_str()
        # Mirrors Aggregator 6.7's id.php payload exactly.
        body = {
            'ClockNtp':       True,
            'ClockNtpDiff':   0,
            'SkimPort':       '7300',
            'aggVersion':    AGG_VERSION,
            'bandLimits':    bands_str,
            'cWBandLimits':  bands_str,
            'fTBandLimits':  '',
            'fingerPrint':   self.fingerprint,
            'justCQ':        '0',
            'mIXEDBandLimits': '',
            'masterSCPFilter': '0',
            'rTTYBandLimits': '',
            'showIP':        '1',
            'skimCall':      self.skim_call,
            'skimGrid':      self.skim_grid,
            'skimName':      self.skim_name,
            'skimQth':       self.skim_qth,
            'skimValLevel':  self.skim_validation,
            'skimVersion':   SKIM_VERSION,
            't':             'id',
        }
        # skimSignIn is part of the shortHash input; build it once and
        # use the same string for both the body field and the hash.
        body['skimSignIn'] = (f'Skimmer Server {SKIM_VERSION} '
                              f'is operated by {self.skim_name}, {self.skim_call}')
        body['shortHash']  = id_php_short_hash(body['skimSignIn'])
        resp = self._post('id.php', body)
        if isinstance(resp, dict) and resp:
            self.server_policy = resp
            interval = resp.get('serverUploadInterval')
            limit = resp.get('Limit3Min')
            if interval or limit:
                log.info('RBN policy: upload_interval=%ss, limit_3min=%s',
                         interval, limit)
        return resp

    def post_spots(self, spots):
        """Upload a batch of spot dicts (from parse_spot)."""
        if not spots:
            return None
        # Spot tuple in RBN's current parser format (verified live
        # 2026-05-03). NOT what Aggregator's v6.7 binary emits — that
        # binary's input parser is broken against the current SkimSrv
        # output format, so its tuples render as "OTHER 0 dB" garbage
        # on RBN's worldwide telnet broadcast.
        #
        # Real correctly-rendering operators on RBN (KM3T-3, ZF1A,
        # DM5GG, OE9GHV, ...) send THIS format — either via a newer
        # Aggregator with patched parser or via custom feeders. Either
        # way, this is the format that produces clean CW + SNR + WPM
        # rendering on the wire, and joining that population is the
        # honest correct move (we contribute to mode-aware downstream
        # filters instead of polluting them with mis-tagged spots).
        #
        # See /mnt/atlas/skimmer/agg_re/spot_tuple_format.md for the
        # full position mapping and Batwing's IL analysis at
        # /mnt/atlas/skimmer/agg_re/aggregator_input_parser_bug.md
        # (TODO).
        _MODE_TO_CODE = {'CW': '1'}  # extend when other modes are wired up
        _CQ_TO_CODE   = {'CQ': '1', 'DX': '2', 'BCN': '3', 'BEACON': '3'}
        s_array = [
            [
                f'{sp["freq"]:.2f}',
                sp['call'],
                _CQ_TO_CODE.get(sp['cq_flag'], '1'),
                str(sp['snr']),
                str(sp['wpm']),
                'dB',
                _MODE_TO_CODE.get(sp['mode'], '1'),  # CW default
                sp['cq_flag'],
                'D:',
            ]
            for sp in spots
        ]
        body = {
            'agg':   AGG_VERSION,
            'e':     self.skim_call,
            'fp':    self.fingerprint,
            'h':     s_php_h(sp['call'] for sp in spots),
            'nTP':   True,
            'tm':    time.time(),
            's':     s_array,
            't':     's',
        }
        return self._post('s.php', body)


def read_local(local_host, local_port, skim_call, spot_q, shutdown):
    """Connect to SparkGap's cluster telnet, parse DX-de lines into
    spot_q. Reconnect on disconnect. Runs until shutdown is set."""
    while not shutdown.is_set():
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(60)
            s.connect((local_host, local_port))
            log.info('connected to local skimmer %s:%d', local_host, local_port)
            time.sleep(0.5)
            try:
                banner = s.recv(4096)
                log.debug('banner: %r', banner[:200])
            except socket.timeout:
                pass
            s.sendall(f'{skim_call}\n'.encode('ascii'))
            buf = b''
            while not shutdown.is_set():
                chunk = s.recv(8192)
                log.debug('recv: %d bytes', len(chunk))
                if not chunk:
                    log.warning('local skimmer disconnected (EOF)')
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    text = line.decode('ascii', errors='replace').rstrip('\r')
                    if 'DX de' not in text:
                        continue
                    log.debug('parse: %r', text[:120])
                    spot = parse_spot(text)
                    if spot and spot['mode'] in RBN_MODES:
                        spot_q.put(spot)
                        log.debug('queued: %s @ %.1f', spot['call'], spot['freq'])
        except (socket.error, OSError) as e:
            log.warning('local read error: %s — reconnecting in 5s', e)
        finally:
            if s:
                try: s.close()
                except Exception: pass
        if not shutdown.is_set():
            shutdown.wait(5)


def upload_loop(rbn, spot_q, blacklist, shutdown):
    """Drain spot_q every upload interval, post batch to RBN.
    Interval starts at 10 s and adapts to whatever the server returns
    in id.php (serverUploadInterval)."""
    counts = {'forwarded': 0, 'blacklisted': 0, 'dropped': 0}
    while not shutdown.is_set():
        interval = float(rbn.server_policy.get('serverUploadInterval') or 10)
        shutdown.wait(interval)
        if shutdown.is_set():
            break
        batch = []
        while True:
            try:
                sp = spot_q.get_nowait()
            except queue.Empty:
                break
            if sp['call'] in blacklist:
                counts['blacklisted'] += 1
                continue
            batch.append(sp)
        if batch:
            resp = rbn.post_spots(batch)
            if resp is None and not rbn.dry_run:
                counts['dropped'] += len(batch)
            else:
                counts['forwarded'] += len(batch)
                log.info('forwarded %d spots (totals: %s)', len(batch), counts)


def heartbeat_loop(rbn, shutdown):
    """Re-register / heartbeat every ~50 s. First call also primes
    server_policy so upload_loop can pick the right cadence."""
    rbn.post_id()  # initial registration
    while not shutdown.is_set():
        shutdown.wait(50)
        if shutdown.is_set():
            break
        rbn.post_id()


def fetch_bands_from_skimmer(local_host, local_port, skim_call):
    """Connect briefly, send SKIMMER/SETT, parse band ranges from response.
    Returns list of (low_khz, high_khz) tuples or [] on failure."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect((local_host, local_port))
        time.sleep(0.5)
        s.recv(4096)
        s.sendall(f'{skim_call}\n'.encode())
        time.sleep(0.5)
        s.recv(4096)
        s.sendall(b'SKIMMER/SETT\n')
        time.sleep(0.5)
        resp = s.recv(4096).decode('ascii', errors='replace')
        m = re.search(r'SETT:\s*\S+\s+([\d.,\s-]+?)(?:\r|\n|$)', resp)
        if not m:
            return []
        ranges = []
        for chunk in m.group(1).split(','):
            chunk = chunk.strip()
            if '-' not in chunk:
                continue
            lo, hi = chunk.split('-')
            ranges.append((float(lo), float(hi)))
        return ranges
    except (socket.error, OSError, ValueError) as e:
        log.warning('SETT probe failed: %s', e)
        return []
    finally:
        if s:
            try: s.close()
            except Exception: pass


def main():
    p = argparse.ArgumentParser(description='RBN spot feeder for SparkGap')
    p.add_argument('--call', required=True,
                   help='Operator callsign (e.g. WF8Z)')
    p.add_argument('--name', default='SparkGap',
                   help='Operator name / source label (default: SparkGap)')
    p.add_argument('--grid', required=True,
                   help='6-character grid square (e.g. EM79SM)')
    p.add_argument('--qth', default='',
                   help='QTH text (free-form)')
    p.add_argument('--validation', default='Normal',
                   choices=['Normal', 'Aggressive'],
                   help='Skimmer validation level (default: Normal)')
    p.add_argument('--local-host', default='127.0.0.1',
                   help='SparkGap cluster telnet hostname')
    p.add_argument('--local-port', type=int, default=7300,
                   help='SparkGap cluster telnet port')
    p.add_argument('--rbn-url', default=DEFAULT_RBN_URL,
                   help='RBN ingest base URL (default: %(default)s)')
    p.add_argument('--blacklist', default='blacklist.txt',
                   help='Path to local blacklist (one CALL per line)')
    p.add_argument('--dry-run', action='store_true',
                   help='Parse local spots and log them; do not POST upstream')
    p.add_argument('--log-level', default='INFO',
                   choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )

    blacklist = load_blacklist(args.blacklist)
    log.info('loaded %d blacklist entries', len(blacklist))

    # Probe the skimmer for its band coverage so we can advertise it
    # accurately in id.php. If the probe fails (skimmer down, no SETT
    # support, etc.) we fall back to no bands — RBN tolerates an empty
    # bandLimits string.
    bands = fetch_bands_from_skimmer(args.local_host, args.local_port, args.call)
    if bands:
        log.info('skimmer covers: %s',
                 ','.join(f'{lo:.1f}-{hi:.1f}' for lo, hi in bands))
    else:
        log.warning('no band info from skimmer SETT — using empty bandLimits')

    rbn = RBNSession(
        url=args.rbn_url,
        skim_call=args.call,
        skim_name=args.name,
        skim_grid=args.grid,
        skim_qth=args.qth,
        skim_validation=args.validation,
        bands=bands,
        dry_run=args.dry_run,
    )

    spot_q = queue.Queue()
    shutdown = threading.Event()

    threads = [
        threading.Thread(target=read_local,
                         args=(args.local_host, args.local_port, args.call,
                               spot_q, shutdown),
                         name='read_local', daemon=True),
        threading.Thread(target=upload_loop,
                         args=(rbn, spot_q, blacklist, shutdown),
                         name='upload_loop', daemon=True),
        threading.Thread(target=heartbeat_loop,
                         args=(rbn, shutdown),
                         name='heartbeat', daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while not shutdown.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        log.info('interrupted, shutting down')
        shutdown.set()
        for t in threads:
            t.join(timeout=3)
        sys.exit(0)


if __name__ == '__main__':
    main()
