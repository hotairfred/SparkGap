#!/usr/bin/env python3
"""
flex_iq.py — FlexRadio 6000 wideband I/Q receiver for SparkGap.

Talks to a FlexRadio 6000/8000 series radio over the SmartSDR TCP API
(port 4992) to set up a DAX-IQ stream at up to 192 kHz, receives
VITA-49 UDP packets, and hands I/Q samples to a caller-supplied
callback in the same shape as hpsdr_receiver.HPSDRReceiver.

Key discovery: the radio requires `client gui <UUID>` registration
before it will allow panadapter or DAX-IQ creation. Without it, the
radio treats the connection as a restricted non-GUI client.

One SparkGap station per radio holds that GUI client (FlexStation, run by
flex_station.py or inside a lone copy); each band's FlexIQReceiver binds to
it with `client bind` and owns only its own pan, slice and DAX-IQ stream.

Protocol refs:
  - AetherSDR source (ten9876/AetherSDR) — RadioModel.cpp, DaxIqModel.cpp
  - AB4EJ-1/FlexRadioIQ — VITA-49 packet struct
  - SmartSDR TCP/IP API wiki
"""

import contextlib
import logging
import os
import re
import select
import signal
import socket
import struct
import threading
import time
import uuid

import numpy as np

log = logging.getLogger("sparkgap.flex")  # under "sparkgap", so the console counts its warnings and errors

# VITA-49 DAX-IQ packet layout
VITA_HEADER_SIZE = 28
SAMPLES_PER_PACKET = 512
DAXIQ_PACKET_SIZE = VITA_HEADER_SIZE + SAMPLES_PER_PACKET * 8  # 4124

VALID_RATES = (24000, 48000, 96000, 192000)

# DAX-IQ floats are int16 full scale; FlexLib VitaIFDataPacket divides by 2**15.
DAXIQ_FULL_SCALE = 32768.0

STATION_NAME = 'SparkGap'   # shown in SmartSDR's station list (Multi-Flex), with the radio's nickname
STALL_S = 10                # no DAX-IQ packet for this long: a new stream
RETRY_S = 30                # after a recovery: time for the new stream to start
LOSS_BUDGET = 3             # losses recovered within LOSS_WINDOW_S; the next stops: no loops on the radio
LOSS_WINDOW_S = 3600
CMD_TIMEOUT_S = 3.0         # removes and recovery: a 1 s wait missed replies that came (2026-10-22 12:47Z)
HEARTBEAT_S = 1.0           # the station pings the radio this often, with keepalive on, as FlexLib does
HEARTBEAT_TIMEOUT_S = 3.0
KEEPALIVE_DROP_S = 20       # no ping reply this long: the radio has dropped us (its keepalive is 15 s)
OUTAGE_MAX_S = 600          # an unreachable radio is tried every RETRY_CONNECT_S for this long, then we stop
RETRY_CONNECT_S = 10
SETTLE_S = 60               # after an outage the radio must answer pings this long in a row before we
                            # recover: it takes a minute or two to settle (Dallas 6400M, 2026-10-22 14:02Z)
STREAM_GRACE_S = 10         # after a stall, time for our stream to resume by itself before a new one
STATION_WAIT_S = 600        # a copy whose own station left waits this long for it before stopping
STATION_TAKEOVER_S = 10     # times the DAX-IQ channel: a copy waits this long for a lost station, then holds it
STATION_POLL_S = 10
LIVE_LOCK_WAIT_S = 2.0      # a live change waits this long for the receiver; a recovery holds it longer
RETUNE_SETTLE_S = 0.5       # IQ dropped this long after a retune's replies: still the old frequency
PAN_MOVED_MHZ = 0.5e-6      # our pan's reported centre further than this (0.5 Hz; it reports 1 Hz) from ours: moved by someone else
TX_STATES = ('PTT_REQUESTED', 'TRANSMITTING', 'UNKEY_REQUESTED')  # the radio's interlock while keyed
TX_HOLD_S = 0.1             # after key-up (READY/RECEIVE): bridges the shortest gaps between words of an over
TX_SPANS_KEPT = 64


def owned_objects(lines, handle):
    """Slice numbers and pan ids the radio reports as belonging to client `handle`."""
    slices, pans = set(), set()
    if not handle:
        return slices, pans
    own = re.compile(r'client_handle=0x' + re.escape(handle) + r'\b', re.I)
    for line in lines:
        if not own.search(line):
            continue
        m = re.search(r'\|slice (\d+) ', line)
        if m and 'in_use=0' not in line:
            slices.add(int(m.group(1)))
        m = re.search(r'\|display pan (0x[0-9A-F]+) ', line, re.I)
        if m:
            pans.add(m.group(1))
    return slices, pans


def gui_stations(lines):
    """GUI clients in the radio's client status lines: [(handle, client_id, program, station)]."""
    out = []
    for line in lines:
        m = re.search(r'\|client (0x[0-9A-F]+) connected (.*)', line, re.I)
        if not m:
            continue
        kv = dict(p.split('=', 1) for p in m.group(2).split() if '=' in p)
        if kv.get('client_id'):
            out.append((m.group(1), kv['client_id'], kv.get('program', ''),
                        kv.get('station', '').replace('\x7f', ' ')))
    return out


def daxiq_holders(lines, channel):
    """Pans bound to DAX-IQ `channel` in pan status lines: {pan id: owner client handle}.
    The radio sends daxiq_channel and client_handle on separate lines; the latest wins."""
    chan, owner = {}, {}
    for line in lines:
        m = re.search(r'\|display pan (0x[0-9A-F]+) (.*)', line, re.I)
        if not m:
            continue
        kv = dict(p.split('=', 1) for p in m.group(2).split() if '=' in p)
        pan = m.group(1).lower()
        if 'daxiq_channel' in kv:
            chan[pan] = kv['daxiq_channel']
        if 'client_handle' in kv:
            owner[pan] = kv['client_handle'].upper()
    return {p: owner.get(p, '') for p, c in chan.items() if c == str(channel) and c != '0'}


def daxiq_streams(lines, out=None):
    """DAX-IQ streams in stream status lines: {stream id: {'channel', 'pan', 'client', 'gui'}}.
    A stream's fields come over several lines; the latest wins and "removed" drops it.
    `out`: streams known so far, updated in place."""
    out = {} if out is None else out
    for line in lines:
        m = re.search(r'\|stream (0x[0-9A-F]+)( .*|$)', line, re.I)
        if not m:
            continue
        sid, rest = int(m.group(1), 16), m.group(2).split()
        if 'removed' in rest:
            out.pop(sid, None)
            continue
        kv = dict(p.split('=', 1) for p in rest if '=' in p)
        if kv.get('type', 'dax_iq' if sid in out else '') != 'dax_iq':
            continue
        if sid not in out and not {'daxiq_channel', 'pan', 'client_handle'} & kv.keys():
            continue
        st = out.setdefault(sid, {})
        if 'daxiq_channel' in kv:
            st['channel'] = kv['daxiq_channel']
        if 'pan' in kv:
            st['pan'] = int(kv['pan'], 16)
        if 'client_handle' in kv:
            st['client'] = int(kv['client_handle'], 16)
        if 'client_gui_handle' in kv:     # the GUI station a DAX program or bound copy streams for
            st['gui'] = int(kv['client_gui_handle'], 16)
    return out


def pan_status(lines):
    """Pans in pan status lines: {pan id: DAX-IQ channel, or None when none is reported}.
    The radio restores a GUI client's saved pans with their old channel (2026-10-20), so a
    channel on a pan says nothing about who streams it: only a DAX-IQ stream does."""
    out = {}
    for line in lines:
        m = re.search(r'\|display pan (0x[0-9A-F]+)( .*|$)', line, re.I)
        if not m:
            continue
        pan, rest = int(m.group(1), 16), m.group(2).split()
        if 'removed' in rest:
            out.pop(pan, None)
            continue
        kv = dict(p.split('=', 1) for p in rest if '=' in p)
        out[pan] = kv.get('daxiq_channel', out.get(pan))
    return out


def pan_owners(lines):
    """Pans in pan status lines: {pan id: owner client handle}, both as numbers."""
    out = {}
    for line in lines:
        m = re.search(r'\|display pan (0x[0-9A-F]+) .*\bclient_handle=(0x[0-9A-F]+)', line, re.I)
        if m:
            out[int(m.group(1), 16)] = int(m.group(2), 16)
    return out


def daxiq_claims(streams, channel, me, sparkgap):
    """DAX-IQ streams on `channel` from clients other than `me` that claim it: bound to a pan, or
    another SparkGap copy's (`sparkgap`: its GUI handles).  A TCI server keeps an unbound stream on
    every channel (Fred's 6700, 2026-10-20); the radio lets another client stream that channel too."""
    return [st for st in streams.values()
            if st.get('channel') == str(channel) and st.get('client') not in (None, me)
            and (st.get('pan') or {st.get('client'), st.get('gui')} & sparkgap)]


def daxiq_users(streams, channel, me, sparkgap):
    """Client handles of the streams that claim `channel` (daxiq_claims)."""
    return {st['client'] for st in daxiq_claims(streams, channel, me, sparkgap)}


def radio_nickname(lines):
    """The radio's nickname from its reply to "info" (name="Dallas"), or '' when it has none."""
    for line in lines:
        m = re.match(r'R\d+\|0\|.*?\bname="([^"]*)"', line)
        if m:
            return m.group(1).strip()
    return ''


def station_for(nickname):
    """SparkGap-<radio nickname>, one station for every copy on that radio;
    SparkGap when the radio has no nickname."""
    nick = re.sub(r'[^A-Za-z0-9_-]+', '-', nickname).strip('-')
    return f"{STATION_NAME}-{nick}" if nick else STATION_NAME


def clients_gone(lines):
    """Status lines in which the radio reports a client disconnected."""
    return [l for l in lines if re.search(r'\|client 0x[0-9A-F]+ disconnected', l, re.I)]


def reply_log(line, sent):
    """(level, message) for a command reply R<seq>|<code>|<text>, naming the command from
    `sent` (seq -> command); None for any other line.  A non-zero code means refused."""
    m = re.match(r'R(\d+)\|([0-9A-Fa-f]+)\|', line)
    if not m:
        return None
    cmd = sent.pop(int(m.group(1)), '?')
    level = logging.INFO if int(m.group(2), 16) == 0 else logging.WARNING
    return level, "[Flex] <<< %s (%s)" % (line[:200], cmd)


def utc_hms(t):
    """time.time() as HH:MM:SSZ, or 'none since connecting' for None."""
    return time.strftime('%H:%M:%SZ', time.gmtime(t)) if t else 'none since connecting'


def tx_states(lines):
    """The radio's interlock states in status lines (S0|interlock ... state=TRANSMITTING), in order."""
    return [m.group(1) for l in lines for m in [re.search(r'\|interlock .*\bstate=([A-Z_]+)', l)] if m]


def ptt_ours(lines):
    """Status lines in which the radio gives a SparkGap station the key and PTT jacks."""
    return [l for l in lines if f'program={STATION_NAME} ' in l
            and re.search(r'\|client 0x[0-9A-F]+ .*\blocal_ptt=1\b', l, re.I)]


def pan_slices(lines, pan_id):
    """Slice numbers the radio reports on panadapter pan_id, whoever owns them."""
    on = re.compile(r'\bpan=0x0*%x\b' % pan_id, re.I)
    return {int(m.group(1)) for line in lines
            if 'in_use=0' not in line and on.search(line)
            for m in [re.search(r'\|slice (\d+) ', line)] if m}


def slice_letter(lines, slice_id):
    """The letter SmartSDR and Maestro show for slice slice_id (its index_letter), or None."""
    tag = re.compile(r'\|slice %d .*\bindex_letter=([A-Z])\b' % slice_id)
    found = [m.group(1) for line in lines for m in [tag.search(line)] if m]
    return found[-1] if found else None


RX_KEYS = ('rxant', 'loopa', 'loopb', 'rfgain')   # our pan's receive path, as the radio reports it
# receive loops by model (FLEX-6000 Hardware Reference Manual 7.17-7.20: RX ANT-A in/out, RX ANT-B on
# the 6700/6700R only).  Other models report loopa=0 loopb=0 with no jacks behind them (6400M, 6600).
LOOPS_BY_MODEL = {'FLEX-6500': ('loopa',), 'FLEX-6700': ('loopa', 'loopb'), 'FLEX-6700R': ('loopa', 'loopb')}


def radio_loops(lines):
    """The receive loops the radio has, from its reply to "info" (model="FLEX-6700")."""
    for line in lines:
        m = re.match(r'R\d+\|0\|.*?\bmodel="([^"]*)"', line)
        if m:
            return LOOPS_BY_MODEL.get(m.group(1).strip(), ())
    return ()


def pan_rx(lines, pan_id, out=None, loops=('loopa', 'loopb')):
    """Our pan's receive path from its status lines, folded into out: rxant, the loops the radio
    has (loops), rfgain and the antennas the pan offers (ant_list).  A loop is shared by every pan on its antenna and
    RF gain by every pan on the same SCU, so another station can change them under us."""
    out = dict(out or {})
    tag = re.compile(r'\|display pan 0x0*%x( .*|$)' % pan_id, re.I)
    for line in lines:
        m = tag.search(line)
        if not m:
            continue
        kv = dict(p.split('=', 1) for p in m.group(1).split() if '=' in p)
        if 'rxant' in kv:
            out['rxant'] = kv['rxant']
        for k in loops:
            if k in kv:
                out[k] = kv[k] == '1'
        if 'rfgain' in kv:
            try:
                out['rfgain'] = int(float(kv['rfgain']))
            except ValueError:
                pass
        if 'ant_list' in kv:
            out['ant_list'] = [a for a in kv['ant_list'].split(',') if a]
    return out


def pan_center(lines, pan_id):
    """The last centre (MHz) the radio reported for panadapter pan_id in these lines, or None."""
    tag = re.compile(r'\|display pan 0x0*%x .*\bcenter=([0-9.]+)' % pan_id, re.I)
    found = [float(m.group(1)) for line in lines for m in [tag.search(line)] if m]
    return found[-1] if found else None


def rfgain_steps(reply):
    """(low, high, step) from the reply to `display pan rfgain_info`, or None."""
    parts = (reply or '').split('|')
    if len(parts) < 3 or parts[1] != '0':
        return None
    try:
        low, high, step = (int(float(v)) for v in parts[2].split(',')[:3])
    except ValueError:
        return None
    return (low, high, step) if step > 0 and high >= low else None


def rx_args(vals):
    """' rxant=ANT1 loopa=1 ...' for a radio command."""
    return ''.join(f' {k}={int(v) if isinstance(v, bool) else v}' for k, v in vals.items() if v is not None)


class Recovering(RuntimeError):
    """A live change refused at once: a recovery from a radio outage holds the receiver."""


class StationExists(RuntimeError):
    """The radio already has this SparkGap station: another station process holds it."""


class LossBudget:
    """Losses in the last LOSS_WINDOW_S: up to LOSS_BUDGET are recovered, the next one stops.
    A routine LAN hiccup every few hours is recovered; a radio in a bad state is not retried
    in a loop (2026-10-22: three DAX-IQ stalls in 14 h at Dallas, each recovered in 10-13 s)."""

    def __init__(self, budget=None, window=None):
        self.budget = LOSS_BUDGET if budget is None else budget
        self.window = LOSS_WINDOW_S if window is None else window
        self.losses = []          # (time.time(), why), newest last

    def take(self, why, now=None):
        """Record a loss; False when the budget is spent and the caller must stop instead."""
        now = time.time() if now is None else now
        self.losses = [l for l in self.losses if now - l[0] < self.window]
        if len(self.losses) >= self.budget:
            return False
        self.losses.append((now, why))
        return True


class FlexConnection:
    """One TCP control connection to the radio: numbered commands, their replies, status lines."""

    def __init__(self, ip, port=4992):
        self.ip = ip
        self.port = port
        self.handle = ''          # this connection's client handle, hex without 0x
        self.version = '?'
        self.lost = None          # why the connection is gone, once it is
        self.missed = 0           # commands that got no reply: the radio not ready, not a refusal
        self.seq = 0
        self._sent = {}           # seq -> command, until its reply arrives
        self._buf = b''
        self._tcp = None

    def open(self):
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.settimeout(10)
        self._tcp.connect((self.ip, self.port))
        hello = self.drain(3)     # version, handle, status flood
        self.handle = next((l[1:].strip().upper() for l in hello if l.startswith('H')), '')
        self.version = next((l[1:].strip() for l in hello if l.startswith('V')), '?')
        log.info("[Flex] Connected to %s, API %s, client handle 0x%s", self.ip, self.version, self.handle)
        return hello

    def fileno(self):
        return self._tcp.fileno()

    def _recv_line(self, timeout=3.0):
        self._tcp.settimeout(timeout)
        while b'\n' not in self._buf:
            try:
                chunk = self._tcp.recv(4096)
            except socket.timeout:
                return ''
            except OSError as e:
                self.lost = "Control connection error: %s" % e
                return ''
            if not chunk:
                self.lost = "The radio closed the control connection"
                return ''
            self._buf += chunk
        idx = self._buf.index(b'\n')
        line = self._buf[:idx].decode('utf-8', errors='replace').strip()
        self._buf = self._buf[idx + 1:]
        self._log_reply(line)
        return line

    def _log_reply(self, line):
        log.debug("[Flex] <<< %s", line)    # every control line, for -v captures
        r = reply_log(line, self._sent)
        if r and not (r[0] == logging.INFO and r[1].endswith('(ping)')):
            log.log(*r)

    def drain(self, timeout=2.0):
        """Read all available lines within timeout."""
        lines = []
        end = time.time() + timeout
        while time.time() < end:
            line = self._recv_line(timeout=max(0.1, end - time.time()))
            if not line:
                break
            lines.append(line)
        return lines

    def send(self, cmd):
        self.seq += 1
        self._sent[self.seq] = cmd
        self._tcp.sendall(f"C{self.seq}|{cmd}\n".encode())
        (log.debug if cmd == 'ping' else log.info)("[Flex] >>> C%d|%s", self.seq, cmd)
        return self.seq

    def cmd(self, cmd, timeout=2.0, settle=0.0):
        """Send a command and read until its own reply R<seq>|... (or timeout),
        then keep reading `settle` seconds for status lines that follow it."""
        seq = self.send(cmd)
        lines = []
        end = time.time() + timeout
        while time.time() < end:
            line = self._recv_line(timeout=max(0.05, end - time.time()))
            if not line:
                if self.lost:
                    break
                continue
            lines.append(line)
            if line.startswith(f"R{seq}|"):
                break
        else:
            self.missed += 1
        if settle and not self.lost:
            lines += self.drain(settle)
        return lines

    def reply(self, resp):
        """The reply line to the last command in resp, or None."""
        return next((l for l in resp if l.startswith(f"R{self.seq}|")), None)

    def ok(self, what, resp):
        """Log OK or FAILED (with the radio's error code) for the last command."""
        reply = self.reply(resp)
        if reply is None:
            log.warning("[Flex] FAILED %s: no reply from the radio", what)
            return False
        code = reply.split('|')[1]
        if code == '0':
            log.info("[Flex] OK %s", what)
            return True
        log.info("[Flex] FAILED %s: radio error %s", what, code)   # the reply line above is the warning
        return False

    def hex_id(self, resp):
        """The hex id in the R<seq>|0|<hex_id> reply to the last command, or None."""
        parts = (self.reply(resp) or '').split('|')
        if len(parts) >= 3 and parts[1] == '0' and parts[2].strip():
            raw = parts[2].strip().split(',')[0].strip()
            for base in (16, 10):
                try:
                    return int(raw, base)
                except ValueError:
                    pass
        return None

    def poll(self):
        """Status lines the radio has sent, without waiting; None when the connection is gone
        (the cause in self.lost).  Unread, its status messages (many while the operator
        transmits) fill the socket buffer and the radio drops the client: 2026-10-13."""
        try:
            chunk = self._tcp.recv(65536, socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError, TimeoutError):
            return []
        except OSError as e:
            self.lost = "Control connection error: %s" % e
            return None
        if not chunk:
            self.lost = "The radio closed the control connection"
            return None
        *lines, self._buf = (self._buf + chunk).split(b'\n')
        lines = [l.decode('utf-8', errors='replace') for l in lines]
        for line in lines:
            self._log_reply(line)
        return lines

    def close(self, reason):
        if self._tcp:
            try:
                self._tcp.sendall(b'\x04')    # FlexLib tells the radio it is leaving before it closes
            except OSError:
                pass
            if self.handle:
                log.info("[Flex] Disconnected from %s (client 0x%s, %s)", self.ip, self.handle, reason)
            try:
                self._tcp.close()
            except OSError:
                pass
            self._tcp = None


def register_program(conn):
    resp = conn.cmd(f"client program {STATION_NAME}")
    if any(l.startswith(f"R{conn.seq}|10000002|") for l in resp):
        # Not a FlexRadio program name; the radio still records it.
        log.info("[Flex] OK program name %s (radio notes it as an unknown program)", STATION_NAME)
    else:
        conn.ok("program name " + STATION_NAME, resp)


def station_name(conn, configured=None):
    """flex_station when set, else SparkGap-<radio nickname> from "info"."""
    if configured:
        return configured
    nickname = radio_nickname(conn.cmd("info"))
    name = station_for(nickname)
    log.info("[Flex] Station %s (radio nickname %s)", name, repr(nickname) if nickname else "none")
    return name


def station_client_id(ip, station):
    """One GUI client ID per radio and station, the same on every start, as FlexLib keeps its own."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sparkgap://{ip}/{station}")).upper()


def find_station(lines, station, ip=None):
    """(handle, client_id) of the SparkGap GUI client named `station` in client status, or None.
    With `ip`, also one still registering: listed by its client ID before it has its name."""
    cid = station_client_id(ip, station) if ip else None
    return next(((g[0], g[1]) for g in gui_stations(lines)
                 if (g[2] == STATION_NAME and g[3] == station) or g[1] == cid), None)


def nap(seconds, stopping):
    """Sleep up to `seconds`, waking early once stopping() is true."""
    end = time.time() + seconds
    while time.time() < end and not stopping():
        time.sleep(min(0.5, max(0.0, end - time.time())))


def await_radio(ip, port, conn=None, stopping=lambda: False, outage=False):
    """Wait for the radio to answer pings.  One that answers at once is ready.  One that does not
    (an outage: a pulled cable, a router reset) must answer SETTLE_S in a row before it is, and while
    it cannot be reached a new connection is tried each RETRY_CONNECT_S, for up to OUTAGE_MAX_S.
    Nothing reaches an unreachable radio, so this is no loop against it.  Returns an open
    connection (`conn` when it lasted) or None.  `outage`: one has already happened (a lost
    connection), so a first answer is not enough: the radio answers before it is ready (14:19Z)."""
    t0 = time.time()
    steady = None
    while time.time() - t0 < OUTAGE_MAX_S and not stopping():
        if conn is None or conn.lost:
            if conn:
                conn.close("radio unreachable")
            conn = FlexConnection(ip, port)
            try:
                conn.open()
            except OSError as e:
                conn, steady, outage = None, None, True
                log.warning("[Flex] Radio %s unreachable for %.0f s: %s", ip, time.time() - t0, e)
                nap(RETRY_CONNECT_S, stopping)
                continue
        if conn.reply(conn.cmd("ping", timeout=HEARTBEAT_TIMEOUT_S)) is None:
            if not outage:
                log.warning("[Flex] Radio %s not answering: waiting for it", ip)
            steady, outage = None, True
            continue
        if not outage:
            conn.missed = 0
            return conn
        if steady is None:
            steady = time.time()
            log.info("[Flex] Radio %s answering again after %.0f s; settling %d s", ip, steady - t0, SETTLE_S)
        if time.time() - steady >= SETTLE_S:
            log.info("[Flex] Radio %s steady, %.0f s after the outage began", ip, time.time() - t0)
            conn.missed = 0     # misses while it settled say nothing about what comes next
            return conn
        nap(HEARTBEAT_S, stopping)
    if conn:
        conn.close("radio did not come back" if not stopping() else "stopping")
    return None


class FlexStation:
    """SparkGap's one GUI client on a radio.  Every band's FlexIQReceiver binds to it, so a copy
    that stops, restarts or loses its connection takes nothing of the others' with it.  Run by
    flex_station.py for a seat of several copies, or inside a lone copy that found none."""

    def __init__(self, ip, control_port=4992, station=None):
        self.ip = ip
        self.control_port = control_port
        self._station_cfg = station
        self.station = station or STATION_NAME
        self.client_id = ''
        self.conn = None
        self.conn_handle = None   # the handle the station last registered under
        self.state = "connecting"
        self.rtt_ms = None
        self.last_ping = None
        self.started = None
        self.channels = []        # DAX-IQ streams the radio reports for this station
        self.budget = LossBudget()
        self._streams = {}
        self._unanswered = None   # time.time() of the first ping in a row without a reply
        self._ptt_warned = False
        self._stop = threading.Event()
        self._thread = None

    def start(self, again=False, conn=None):
        """Register the station.  At first start, raises StationExists when the radio already has it;
        `again` (after a loss) registers over our own stale entry with the same client id.
        `conn`: a connection already open (after await_radio)."""
        try:
            if conn is None:
                conn = FlexConnection(self.ip, self.control_port)
                conn.open()
            self.station = station_name(conn, self._station_cfg)
            self.client_id = station_client_id(self.ip, self.station)
            register_program(conn)
            clients = conn.cmd("sub client all", settle=0.5)
            found = find_station(clients, self.station, self.ip)
            # after a loss, a station under another handle is a copy that took our place: leave it to that copy
            if found and not (again and self.conn_handle and int(found[0], 16) == int(self.conn_handle, 16)):
                raise StationExists(f"[Flex] The radio already has station {self.station}")
            # A GUI client's saved pans come back when it registers (2026-10-20); the station keeps none.
            conn.ok("persistence off", conn.cmd("client start_persistence off"))
            resp = conn.cmd(f"client gui {self.client_id}", timeout=5.0)
            if not conn.ok("GUI client", resp):
                if conn.reply(resp) is None:
                    raise RuntimeError("[Flex] No reply to the GUI client registration")
                raise RuntimeError("[Flex] The radio refused the GUI client: both GUI slots in use?")
            if self._stop.is_set():           # stopped meanwhile: leave no GUI client behind
                raise RuntimeError("[Flex] Station stopping")
            conn.ok("station name " + self.station, conn.cmd(f"client station {self.station}"))
            conn.ok("keepalive", conn.cmd("keepalive enable"))
            self._streams = daxiq_streams(conn.cmd("sub daxiq all", settle=0.5))
            self._clear_defaults(conn)
        except Exception:
            conn.close("failed start")
            raise
        self.conn = conn
        self.conn_handle = conn.handle
        self._unanswered = None
        self.started = self.started or time.time()
        self.state = "live"
        log.info("[Flex] Station %s holds the GUI client (client 0x%s)", self.station, conn.handle)

    def _clear_defaults(self, conn):
        """Remove the pan and slice the radio gives a new GUI client: the station streams nothing."""
        status = conn.cmd("sub pan all", settle=0.5) + conn.cmd("sub slice all", settle=0.5)
        slices, pans = owned_objects(status, conn.handle)
        for n in sorted(slices):
            conn.ok(f"default slice {n} removed", conn.cmd(f"slice remove {n}", timeout=CMD_TIMEOUT_S))
        for p in sorted(pans):
            conn.ok(f"default pan {p} removed", conn.cmd(f"display pan remove {p}", timeout=CMD_TIMEOUT_S))

    def start_thread(self):
        self._thread = threading.Thread(target=self.run, name="flex-station", daemon=True)
        self._thread.start()

    def run(self, on_beat=None):
        """Hold the station until stop(): ping each HEARTBEAT_S, read status, and register again
        after a loss while the budget lasts.  on_beat(health) after each heartbeat."""
        try:
            while not self._stop.is_set():
                why = self._beat()
                if self._stop.is_set():
                    return
                if why and not self._recover(why):
                    return
                if on_beat:
                    on_beat(self.health())
                self._stop.wait(HEARTBEAT_S)
        finally:
            if self._stop.is_set() and self.conn:   # a registration that finished after stop()
                self.conn.close("stopping")
                self.conn = None

    def _beat(self):
        """One heartbeat; the reason the station is lost, or None.  Unanswered pings are a hiccup to
        ride out, not a loss: the radio decides (its keepalive drops a client after 15 s without a
        ping, then closes the connection).  Leaving on our own would take every copy's pan with us
        (2026-10-22 14:02Z: the station left after 11 s of a 17 s outage the radio rode out)."""
        t0 = time.time()
        try:
            lines = self.conn.cmd("ping", timeout=HEARTBEAT_TIMEOUT_S)
        except OSError as e:
            return "Control connection error: %s" % e
        self._status(lines)
        if self.conn.lost:
            return self.conn.lost
        if self.conn.reply(lines) is None:
            if self._unanswered is None:
                self._unanswered = t0
                log.warning("[Flex] Station %s: radio not answering pings", self.station)
            gone = time.time() - self._unanswered
            return "No ping reply for %.0f s" % gone if gone >= KEEPALIVE_DROP_S else None
        if self._unanswered is not None:
            log.warning("[Flex] Station %s: radio answering again after %.0f s", self.station, t0 - self._unanswered)
            self._unanswered = None
        self.rtt_ms = round((time.time() - t0) * 1000, 1)
        self.last_ping = time.time()
        return None

    def _status(self, lines):
        self._streams = daxiq_streams(lines, self._streams)
        me = int(self.conn.handle, 16) if self.conn and self.conn.handle else None
        self.channels = sorted(({'channel': st.get('channel'), 'pan': st.get('pan'), 'client': st.get('client')}
                                for st in self._streams.values() if st.get('gui') == me),
                               key=lambda c: str(c['channel']))
        for line in clients_gone(lines):
            log.info("[Flex] Radio reports: %s", line.strip()[:200])
        if ptt_ours(lines) and not self._ptt_warned:
            self._ptt_warned = True
            log.warning("[Flex] The radio's key and PTT jacks now follow the SparkGap station, "
                        "not the operator's: set local PTT in SmartSDR")

    def _recover(self, why, now=None):
        """Register again after a loss; False (station halted) when the budget is spent or it fails."""
        if self.conn:
            self.conn.close("reconnecting")
        self.conn = None
        if not self.budget.take(why, now):
            return self._halt("%s, loss %d within %d min" % (why, self.budget.budget + 1, self.budget.window // 60))
        log.error("[Flex] Station %s: %s: registering again once the radio is steady", self.station, why)
        self.state = "reconnecting"
        end = time.time() + OUTAGE_MAX_S
        while True:
            conn = await_radio(self.ip, self.control_port, stopping=self._stop.is_set, outage=True)
            if conn is None:
                return self._halt("the radio did not come back within %d min" % (OUTAGE_MAX_S // 60))
            try:
                self.start(again=True, conn=conn)
                break
            except StationExists:
                return self._halt("a copy holds the station now")
            except Exception as e:       # no reply: not ready yet, so wait again; a refusal stops
                if not conn.missed or self._stop.is_set() or time.time() > end:
                    return self._halt("registering again failed: %s" % e)
                log.warning("[Flex] Station %s: the radio is not ready (%s): waiting again", self.station, e)
        log.info("[Flex] Station %s registered again", self.station)
        return True

    def _halt(self, why):
        log.error("[Flex] Station %s: %s: leaving the radio alone until it is started again", self.station, why)
        self.state = "halted"
        self._stop.set()
        return False

    def request_stop(self):
        """Make run() return after the heartbeat in progress; safe from a signal handler."""
        self._stop.set()

    def stop(self):
        """Drop the GUI client: the radio removes every pan of the station with it."""
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=HEARTBEAT_TIMEOUT_S + RETRY_CONNECT_S + 5)
        if self.conn:
            self.conn.close("stopping")
            self.conn = None
        if self.state != "halted":
            self.state = "stopped"
        log.info("[Flex] Station %s stopped", self.station)

    def health(self):
        return {'type': 'flex-station', 'address': self.ip, 'station': self.station, 'state': self.state,
                'rtt_ms': self.rtt_ms, 'last_ping': self.last_ping, 'started': self.started,
                'unanswered_since': self._unanswered,
                'losses': [[round(t), why] for t, why in self.budget.losses], 'channels': list(self.channels)}


class FlexIQReceiver:
    """DAX-IQ receiver for a FlexRadio 6000/8000 series: one band's channel on the radio.

    Binds to the radio's SparkGap station (FlexStation) and owns exactly one pan, the slice on it
    and one DAX-IQ stream; it removes only those.  Drop-in replacement for HPSDRReceiver, same
    callback contract: callback(rx_index, iq_samples), iq_samples an (n, 2) array.

    Usage:
        rx = FlexIQReceiver('192.168.1.238', freq_hz=7040000, sample_rate=192000)
        rx.start()
        rx.receive(callback)   # blocks
        rx.stop()
    """

    def __init__(self, ip, freq_hz=7040000, sample_rate=192000,
                 daxiq_channel=1, udp_port=7791, control_port=4992, station=None,
                 rxant=None, loopa=None, loopb=None, rfgain=None):
        if sample_rate not in VALID_RATES:
            raise ValueError(f"sample_rate must be one of {VALID_RATES}")
        self.ip = ip
        self.freq_hz = freq_hz
        self.freq_mhz = freq_hz / 1e6
        self.sample_rate = sample_rate
        self.channel = daxiq_channel
        self._station_cfg = station  # flex_station; else SparkGap-<radio nickname>, set on connect
        # flex_rxant / flex_loopa / flex_loopb / flex_rfgain: the receive path our pan starts with;
        # None leaves the radio's (band-persistence) value.  A receive loop is shared by every pan on
        # that antenna and RF gain by every pan on the same SCU, so a pan that comes up with the loop
        # off turns it off for whatever hangs on the loop.  The radio's state is what we show; the target
        # moves only on our own live change (set_rx), so a reconnect restores ours, not another client's.
        self._target = {'rxant': rxant, 'loopa': None if loopa is None else bool(loopa),
                        'loopb': None if loopb is None else bool(loopb),
                        'rfgain': None if rfgain is None else int(rfgain)}
        self.rx = {}                  # our pan's receive path as the radio last reported it (pan_rx)
        self.loops = ()               # the receive loops this radio has (radio_loops), set on connect
        self.rfgain_range = None      # (low, high, step) from display pan rfgain_info
        self.station = station or STATION_NAME
        self.udp_port = udp_port
        self.control_port = control_port
        self.frequencies = [freq_hz]  # match HPSDRReceiver interface

        self.conn = None
        self._udp = None
        self._running = False
        self._stream_id = None
        self._pan_id = None
        self._slice_id = None
        self.slice_letter = None        # the letter SmartSDR shows for our slice (slice_letter)
        self._stream_handle = None    # the client handle that made our stream
        self._pan_owner = None        # the station handle that owned our pan when we made it
        self._station_handle = None   # the station's GUI client handle, as a number
        self._station_lost = False
        self._own_station = None      # the FlexStation this copy holds when it found none on the radio
        self._start_missed = False    # the last failed start had a command without a reply
        self._clients = []            # client status at the last connect
        self._discard_until = 0.0     # IQ before this time is the old frequency (retune)
        self.last_packet = None       # time.time() of the newest DAX-IQ packet since the last stream

        # for the GUI status page (health())
        self.state = "connecting"
        self.packets = 0
        self.lost = 0
        self.reconnects = 0
        self.last_reconnect = None
        self.attempts = 0         # connection attempts, first start and reconnects
        self.budget = LossBudget()
        self._stopping = False
        self._lock = threading.RLock()  # one user of the control connection at a time
        self._ptt_warned = False
        self.others = []          # other stations on the radio at the last connect
        self._tx_since = None     # arrival time of the key-down in progress
        self._tx_spans = []       # (key-down, key-up) arrival times, newest last
        self.muted_packets = 0

    @property
    def rx_path(self):
        """' rxant=.. loopa=.. loopb=..' for our pan and slice, from the target."""
        return rx_args({k: self._target[k] for k in ('rxant', *self.loops)})

    # -- lifecycle -----------------------------------------------------------

    def start(self, why=None):
        """Connect, bind to the station and set up our pan, slice and DAX-IQ stream.
        `why`: the reason for a reconnect."""
        self.last_packet = None
        self._ptt_warned = False
        self._tx_since = None
        self._station_lost = False
        self.attempts += 1
        log.info("[Flex] Connection attempt %d to %s:%d (%s)", self.attempts, self.ip, self.control_port,
                 "reconnect: %s" % why if why else "first start")
        try:
            self._connect(first=why is None)
            if self._stream_id or self._pan_id:    # left by a lost connection; the station kept them
                self._remove_objects()
            self._build()
        except Exception as e:
            log.error("[Flex] Connection attempt %d failed: %s", self.attempts, e)
            self._start_missed = bool(self.conn and self.conn.missed)   # no reply: the radio not ready
            self._remove_objects()
            self._close("failed start")
            if why is None and self._own_station:
                self._own_station.stop()
                self._own_station = None
            raise

    def _connect(self, first):
        """Open the control connection and bind to the station.  A copy that finds no station holds one
        itself: at once at its first start, and after a loss once the station has stayed away for
        STATION_TAKEOVER_S times its DAX-IQ channel, so copies never race for the GUI client
        (2026-10-21 23:19Z) and the copies left keep running when the one holding it stops."""
        if self._own_station and self._own_station.state == "halted":   # evicted, or gave up: hold no more
            self._own_station.stop()
            self._own_station = None
        conn = self.conn = FlexConnection(self.ip, self.control_port)
        conn.open()
        self.loops = radio_loops(conn.cmd("info"))
        dropped = [k for k in ('loopa', 'loopb') if self._target[k] is not None and k not in self.loops]
        if dropped:
            log.warning("[Flex] flex_%s set, but this radio model has no such loop: not sent", "/flex_".join(dropped))
        self.station = station_name(conn, self._station_cfg)
        register_program(conn)
        self._clients = conn.cmd("sub client all", settle=0.5)
        found = find_station(self._clients, self.station, self.ip)
        if not found and not first:
            found = self._wait_for_station()
        if not found and not self._own_station:
            station = FlexStation(self.ip, self.control_port, self._station_cfg)
            try:
                station.start()
                station.start_thread()
                self._own_station = station
                log.info("[Flex] No station %s was on the radio: this copy holds it", self.station)
            except StationExists:       # another copy got there first: bind to its station
                log.info("[Flex] Station %s appeared meanwhile: attaching to it", self.station)
            self._clients = conn.cmd("sub client all", settle=0.5)
            found = find_station(self._clients, self.station, self.ip)
        if not found:
            raise RuntimeError(f"[Flex] Station {self.station} is not on the radio")
        handle, client_id = found
        if not conn.ok(f"attached to station {self.station}", conn.cmd(f"client bind client_id={client_id}")):
            raise RuntimeError(f"[Flex] The radio refused to attach us to station {self.station}")
        self._station_handle = int(handle, 16)
        conn.ok(f"UDP port {self.udp_port} for DAX-IQ", conn.cmd(f"client udpport {self.udp_port}"))

    def _wait_for_station(self):
        """The station after a loss: wait for it to register again, STATION_WAIT_S when this copy holds
        it, else STATION_TAKEOVER_S times our channel before taking its place."""
        wait = STATION_WAIT_S if self._own_station else STATION_TAKEOVER_S * max(1, self.channel)
        end = time.time() + wait
        log.info("[Flex] Waiting up to %d s for station %s", wait, self.station)
        while not self._stopping and not self.conn.lost:
            self._clients = self.conn.cmd("sub client all", settle=0.5)
            found = find_station(self._clients, self.station, self.ip)
            if found or time.time() >= end:     # the last look is at the deadline, not a poll before it
                return found
            nap(min(STATION_POLL_S, end - time.time()), lambda: self._stopping)
        return None

    def _build(self):
        """Our pan, the slice on it and our DAX-IQ stream, each created by this copy."""
        conn = self.conn
        me = int(conn.handle, 16)
        num = lambda h: int(h, 16) if h else None   # handles compare as numbers, not text
        for g in gui_stations(self._clients):
            if num(g[0]) == self._station_handle:
                continue
            log.info("[Flex] Other station on the radio: '%s' (program %s, client %s)", g[3], g[2], g[0])
        self.others = [f"{g[3]} ({g[2]})" for g in gui_stations(self._clients) if num(g[0]) != self._station_handle]
        status = (conn.cmd("sub pan all", settle=0.5) + conn.cmd("sub slice all", settle=0.5)
                  + conn.cmd("sub daxiq all", settle=0.5))
        conn.ok("transmit state for the GUI", conn.cmd("sub tx all", settle=0.3))  # key-down: an empty spectrum
        log.info("[Flex] Decoding muted while the radio is keyed, until %d ms after its key-up",
                 round(TX_HOLD_S * 1000))
        # A DAX-IQ channel another station's pan holds (Maestro, SmartSDR, DAX) is not ours to take,
        # nor one another client streams (another copy set to the same channel included).
        stations = gui_stations(self._clients)
        sparkgap = {num(g[0]) for g in stations if g[2] == STATION_NAME}
        streams = daxiq_streams(status)
        taken = {p: h for p, h in daxiq_holders(status, self.channel).items() if num(h) != self._station_handle}
        claims = daxiq_claims(streams, self.channel, me, sparkgap)
        if taken or claims:
            if taken:
                pan, h = sorted(taken.items())[0]
            else:
                st = min(claims, key=lambda st: st['client'])
                pan = f"0x{st['pan']:08x}" if st.get('pan') else "none"
                h = f"0x{st.get('gui') or st['client']:08X}"
            who = next((f"{g[3]} ({g[2]})" for g in stations if num(g[0]) == num(h)), f"client {h}")
            raise RuntimeError(f"[Flex] DAX-IQ {self.channel} is in use by {who} on pan {pan}; "
                               "set another flex_daxiq_channel or free it there")
        # A copy that died without cleaning up (kill -9, OOM) leaves its pan and slice with the station:
        # a station pan on our channel that no stream feeds is that copy's; remove it before making ours.
        # Judged again on a fresh status just before removing: a pan a copy starting now just made only
        # takes the channel when its stream is bound to it, so it never matches.
        def orphans(lines):
            fed = {st.get('pan') for st in daxiq_streams(lines).values()}
            return [p for p, h in sorted(daxiq_holders(lines, self.channel).items())
                    if num(h) == self._station_handle and int(p, 16) not in fed]
        if orphans(status):
            now = (conn.cmd("sub pan all", settle=0.3) + conn.cmd("sub daxiq all", settle=0.5)   # pans first:
                   + conn.cmd("sub slice all", settle=0.3))                                    # a bound pan reads fed
            for pan in orphans(now):
                for n in sorted(pan_slices(now, int(pan, 16))):
                    conn.ok(f"slice {n} left by a stopped copy removed", conn.cmd(f"slice remove {n}", timeout=CMD_TIMEOUT_S))
                conn.ok(f"pan {pan} left by a stopped copy removed",
                        conn.cmd(f"display pan remove {pan}", timeout=CMD_TIMEOUT_S))

        self._pan_id = conn.hex_id(conn.cmd("display pan create x=1 y=1"))
        if self._pan_id is None:
            raise RuntimeError("[Flex] No free panadapter on the radio; close one in SmartSDR or on the front panel")
        self._pan_owner = self._station_handle
        log.info("[Flex] Pan created: 0x%08x", self._pan_id)
        bw_mhz = self.sample_rate / 1e6
        if not conn.ok(f"pan 0x{self._pan_id:08x} center {self.freq_mhz:.6f} MHz, bandwidth {bw_mhz:.3f} MHz",
                       conn.cmd(f"display pan set 0x{self._pan_id:08x} "
                                f"center={self.freq_mhz:.6f} bandwidth={bw_mhz:.6f}{self.rx_path}"
                                f"{rx_args({'rfgain': self._target['rfgain']})}")):
            raise RuntimeError(f"[Flex] The radio refused to set pan 0x{self._pan_id:08x}")

        # One slice keeps the RF path live (without one the Flex streams zeros).  The radio puts a
        # band-default slice on a new pan; only slices on our own pan are ours (2026-10-22: a station-wide
        # pick took another copy's slice and moved its pan).  dax=0: we need no DAX audio channel.
        slices = pan_slices(conn.cmd("sub slice all", settle=0.5), self._pan_id)
        self._slice_id = min(slices) if slices else None
        if self._slice_id is not None:
            # the radio keeps a band's slice lock: a new pan's slice can come up locked (50000068)
            conn.ok(f"slice {self._slice_id} unlocked", conn.cmd(f"slice unlock {self._slice_id}"))
            if not conn.ok(f"slice {self._slice_id} tuned to {self.freq_mhz:.6f} MHz",
                           conn.cmd(f"slice tune {self._slice_id} {self.freq_mhz:.6f}")):
                raise RuntimeError(f"[Flex] The radio refused to tune slice {self._slice_id}")
            conn.ok(f"slice {self._slice_id} mode CW", conn.cmd(f"slice set {self._slice_id} mode=CW"))
            conn.ok(f"slice {self._slice_id} DAX audio off", conn.cmd(f"slice set {self._slice_id} dax=0"))
            for n in sorted(slices - {self._slice_id}):
                conn.ok(f"extra slice {n} on our pan removed", conn.cmd(f"slice remove {n}", timeout=CMD_TIMEOUT_S))
        else:
            resp = conn.cmd(f"slice create pan=0x{self._pan_id:08x} freq={self.freq_mhz:.6f} mode=CW")
            parts = (conn.reply(resp) or '').split('|')
            self._slice_id = int(parts[2]) if len(parts) >= 3 and parts[1] == '0' and parts[2].strip().isdigit() else None
            if self._slice_id is None:
                raise RuntimeError(f"[Flex] The radio refused a slice on pan 0x{self._pan_id:08x}")
            log.info("[Flex] Slice created: %d", self._slice_id)
        # unmuted, the operator hears it in the radio's headphones; the key must never land on it
        conn.ok(f"slice {self._slice_id} muted", conn.cmd(f"slice set {self._slice_id} audio_mute=1"))
        conn.ok(f"slice {self._slice_id} not a transmit slice", conn.cmd(f"slice set {self._slice_id} tx=0"))
        # the slice carries its own copy of the loop settings
        if self.rx_path and not conn.ok(f"slice {self._slice_id}{self.rx_path}",
                                        conn.cmd(f"slice set {self._slice_id}{self.rx_path}")):
            raise RuntimeError(f"[Flex] The radio refused{self.rx_path} on slice {self._slice_id}")
        # locked, a click on its VFO (SmartSDR, Maestro, front panel) cannot move it or our pan (FlexLib Slice.Lock)
        conn.ok(f"slice {self._slice_id} locked", conn.cmd(f"slice lock {self._slice_id}"))
        self.slice_letter = slice_letter(conn.cmd("sub slice all", settle=0.3), self._slice_id)
        # what the radio now reports for our pan's receive path, and the RF gain steps it offers
        self.rx = pan_rx(conn.cmd("sub pan all", settle=0.3), self._pan_id, loops=self.loops)
        self._target.update({k: self.rx[k] for k in RX_KEYS if self._target[k] is None and k in self.rx})  # unset: as first found
        self.rfgain_range = rfgain_steps(conn.reply(conn.cmd(f"display pan rfgain_info 0x{self._pan_id:08x}")))
        log.info("[Flex] Receive path:%s (antennas %s; RF gain %s)", rx_args({k: self.rx.get(k) for k in RX_KEYS}),
                 ','.join(self.rx.get('ant_list', [])) or "not reported",
                 "%d to %d in steps of %d" % self.rfgain_range if self.rfgain_range else "range not reported")

        self._make_stream()
        if self._udp is None:
            self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            self._udp.bind(('', self.udp_port))
        self._running = not self._stopping
        self.state = "live"
        log.info("[Flex] Ready: station %s (client 0x%s), pan 0x%08x at %.6f MHz x %.3f MHz, "
                 "slice %s CW, DAX-IQ channel %d stream 0x%08x at %d Hz to UDP %d",
                 self.station, conn.handle, self._pan_id, self.freq_mhz, bw_mhz, self._slice_id,
                 self.channel, self._stream_id, self.sample_rate, self.udp_port)

    def _make_stream(self):
        """Create our DAX-IQ stream, bind it to our pan and set its rate."""
        conn = self.conn
        self._stream_id = conn.hex_id(conn.cmd(f"stream create type=dax_iq daxiq_channel={self.channel}"))
        if self._stream_id is None:
            raise RuntimeError("[Flex] Failed to create DAX-IQ stream")
        self._stream_handle = int(conn.handle, 16)
        log.info("[Flex] DAX-IQ stream: 0x%08x", self._stream_id)
        # Bind DAX-IQ to the panadapter: THE critical step
        if not conn.ok(f"DAX-IQ channel {self.channel} bound to pan 0x{self._pan_id:08x}",
                       conn.cmd(f"display pan set 0x{self._pan_id:08x} daxiq_channel={self.channel}")):
            raise RuntimeError(f"[Flex] The radio refused DAX-IQ channel {self.channel} on pan 0x{self._pan_id:08x}")
        # Set sample rate after binding; newer firmware rejects it on an unbound stream
        resp = conn.cmd(f"stream set 0x{self._stream_id:08x} daxiq_rate={self.sample_rate}")
        if not any(l.startswith(f"R{conn.seq}|0|") for l in resp):
            raise RuntimeError(f"[Flex] Radio refused daxiq_rate={self.sample_rate}: {resp[-1:]}")
        log.info("[Flex] DAX-IQ streaming at %d Hz on UDP port %d", self.sample_rate, self.udp_port)

    def _new_stream(self, why):
        """A stalled stream on a live connection: remove ours, confirm it is gone, make a new one.
        The station, pan and slice stay, so no other copy is touched (2026-10-22 Codex review)."""
        conn = self.conn
        log.error("[Flex] %s: new DAX-IQ stream", why)

        def streams():   # no reply is not "not listed" (2026-10-22 14:02Z: that made a second stream)
            lines = conn.cmd("sub daxiq all", timeout=CMD_TIMEOUT_S, settle=0.5)
            if conn.reply(lines) is None:
                raise RuntimeError("[Flex] No reply to the stream status: not making a second stream")
            return daxiq_streams(lines)

        if self._stream_id is not None:
            ours = streams().get(self._stream_id)
            if ours and ours.get('client') == self._stream_handle:
                conn.ok(f"DAX-IQ stream 0x{self._stream_id:08x} removed",
                        conn.cmd(f"stream remove 0x{self._stream_id:08x}", timeout=CMD_TIMEOUT_S))
                again = streams().get(self._stream_id)
                if again and again.get('client') == self._stream_handle:
                    raise RuntimeError(f"[Flex] DAX-IQ stream 0x{self._stream_id:08x} would not go")
        self._stream_id = None
        self._make_stream()

    def _stream_resumed(self):
        """Whether our stream's packets come back by themselves within STREAM_GRACE_S: after a
        hiccup the radio still lists the stream and may carry on with it."""
        end = time.time() + STREAM_GRACE_S
        self._udp.settimeout(0.2)
        try:
            while time.time() < end and not self._stopping:
                try:
                    data, _ = self._udp.recvfrom(DAXIQ_PACKET_SIZE + 128)
                except (socket.timeout, BlockingIOError, InterruptedError):
                    continue
                if len(data) >= 8 and struct.unpack(">I", data[4:8])[0] == self._stream_id:
                    return True
        finally:
            self._udp.settimeout(None)
        return False

    def _close(self, reason):
        if self.conn:
            self.conn.close(reason)
        self.conn = None
        if self._udp:
            try:
                self._udp.close()
            except OSError:
                pass
            self._udp = None

    def _poll(self):
        """Read what the radio has sent on the control connection; None when the connection is gone."""
        lines = self.conn.poll()
        if lines is None:
            return None
        for state in tx_states(lines):
            self._note_tx(state, time.time())
        for line in clients_gone(lines):
            log.info("[Flex] Radio reports: %s", line.strip()[:200])
            m = re.search(r'\|client (0x[0-9A-F]+) disconnected', line, re.I)
            if m and int(m.group(1), 16) == self._station_handle:
                self._station_lost = True
        if self._slice_id is not None:
            self.slice_letter = slice_letter(lines, self._slice_id) or self.slice_letter
        if self._pan_id is not None:
            self._note_rx(pan_rx(lines, self._pan_id, self.rx, self.loops))
            centre = pan_center(lines, self._pan_id)
            # our own retune holds this lock and updates freq_mhz before _poll can read again
            if centre is not None and self.state == "live" and abs(centre - self.freq_mhz) > PAN_MOVED_MHZ:
                log.warning("[Flex] Our pan was moved to %.6f MHz (ours %.6f) by another client", centre, self.freq_mhz)
                self._halt("pan moved to %.6f MHz by another client: its spots would carry the wrong frequency" % centre)
                return lines
        if ptt_ours(lines) and not self._ptt_warned:
            self._ptt_warned = True
            log.warning("[Flex] The radio's key and PTT jacks now follow the SparkGap station, "
                        "not the operator's: set local PTT in SmartSDR")
        return lines

    def _note_rx(self, rx):
        """The radio reported our pan's receive path: shown as it is; the target stays ours (set_rx)."""
        changed = {k: rx[k] for k in RX_KEYS if k in rx and rx.get(k) != self.rx.get(k)}
        if changed and self.rx:
            log.info("[Flex] Our pan now reports%s", rx_args(changed))
        self.rx = rx
        self._target.update({k: rx[k] for k in RX_KEYS if self._target[k] is None and k in rx})  # unset: as first found

    @contextlib.contextmanager
    def _live_lock(self):
        """The receiver's lock for a live change, or Recovering after LIVE_LOCK_WAIT_S: an outage
        holds it for minutes, and the change must not hold up the caller that long."""
        if not self._lock.acquire(timeout=LIVE_LOCK_WAIT_S):
            raise Recovering("recovering from a radio outage: try again once it is back")
        try:
            yield
        finally:
            self._lock.release()

    def set_rx(self, change):
        """Change our pan's receive path in place: any of rxant (one the pan offers), loopa, loopb
        (true or false), rfgain (in the radio's steps).  What the radio then reports is what we run
        with; a refusal puts back what it reported before.  Returns a line for the GUI; raises
        ValueError for a value the radio does not offer, RuntimeError when the radio refuses."""
        with self._live_lock():
            conn, pan, sl = self.conn, self._pan_id, self._slice_id
            if self.state != "live" or not conn or pan is None or sl is None:
                raise RuntimeError("[Flex] Not live: nothing to change")
            want = {}
            for k, v in change.items():
                if k == 'rxant':
                    ants = self.rx.get('ant_list') or []
                    if v not in ants:
                        raise ValueError(f"antenna {v!r} is not one this pan offers ({', '.join(ants) or 'none reported'})")
                elif k in ('loopa', 'loopb'):
                    if k not in self.rx:   # only a loop this model has, as reported for our pan
                        raise ValueError(f"this radio has no {k}")
                    if not isinstance(v, bool):
                        raise ValueError(f"{k} must be true or false")
                elif k == 'rfgain':
                    r = self.rfgain_range
                    if r is None:
                        raise ValueError("the radio reports no RF gain range for our pan")
                    if isinstance(v, bool) or not isinstance(v, int) or not r[0] <= v <= r[1] or (v - r[0]) % r[2]:
                        raise ValueError(f"RF gain must be {r[0]} to {r[1]} in steps of {r[2]}")
                else:
                    raise ValueError(f"{k} cannot be changed live")
                want[k] = v
            if not want:
                raise ValueError("nothing to change")
            old = {k: self.rx[k] for k in want if k in self.rx}

            def send(vals):
                lines = conn.cmd(f"display pan set 0x{pan:08x}{rx_args(vals)}", settle=0.3)
                ok = conn.ok(f"pan 0x{pan:08x}{rx_args(vals)}", lines)
                path = {k: v for k, v in vals.items() if k != 'rfgain'}   # the slice keeps its own copy
                if ok and path:
                    more = conn.cmd(f"slice set {sl}{rx_args(path)}", settle=0.3)
                    ok = conn.ok(f"slice {sl}{rx_args(path)}", more)
                    lines += more
                self._note_rx(pan_rx(lines, pan, self.rx, self.loops))
                return ok

            if send(want):
                self._target.update({k: self.rx.get(k, v) for k, v in want.items()})  # as the radio reported
                return "receive path" + rx_args({k: self.rx.get(k, want[k]) for k in want})
            back = not old or send(old)
            raise RuntimeError(f"[Flex] The radio refused{rx_args(want)}"
                               + ("" if back else "; what it had before could not be put back"))

    def _note_tx(self, state, now):
        """Key-down and key-up times from the interlock state, for the GUI's spectrum."""
        if state in TX_STATES:
            if self._tx_since is None:
                self._tx_since = now
        elif self._tx_since is not None:
            self._tx_spans = (self._tx_spans + [(self._tx_since, now)])[-TX_SPANS_KEPT:]
            self._tx_since = None

    def transmitting_at(self, t):
        """Whether the radio was keyed at time t (seconds since the epoch, as the IQ arrived), with
        TX_HOLD_S after each key-up: the decoders (and so the spectrum) get silence. Key-up is the
        interlock's READY, which comes after the transmitting client's own CW delay (a per-client
        setting we cannot read)."""
        since = self._tx_since
        if since is not None and t >= since:
            return True
        hold, spans = TX_HOLD_S, self._tx_spans
        if not spans or t > spans[-1][1] + hold:   # after the newest key-up: every packet not keyed
            return False
        return any(a <= t <= b + hold for a, b in reversed(spans))

    def _recover(self, why, kind):
        """Recover from a loss while the budget lasts; stop SparkGap when it is spent or recovery fails.
        kind: 'stream' (stall, connection alive): a new stream only; 'link' (connection lost): connect
        again and bind; 'station' (the station left, taking our pan): wait for it, bind, build again."""
        with self._lock:
            if self._stopping:
                return
            now = time.time()
            if not self.budget.take(why, now):
                self._halt("%s, loss %d within %d min" % (why, self.budget.budget + 1, self.budget.window // 60),
                           alive=kind == 'stream')
                return
            self.state = "reconnecting"
            self.reconnects += 1
            self.last_reconnect = round(now)
            log.info("RBN down: %s", time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now)))
            stopping = lambda: self._stopping
            settled = False
            try:
                if kind == 'stream':           # the link may be down too: wait until the radio answers
                    log.error("[Flex] %s: waiting for the radio", why)
                    conn = await_radio(self.ip, self.control_port, conn=self.conn, stopping=stopping)
                    if conn is None:
                        self.conn = None
                        self._halt("the radio did not come back within %d min" % (OUTAGE_MAX_S // 60), alive=False)
                        return
                    settled = True
                    station = find_station(conn.cmd("sub client all", settle=0.5), self.station, self.ip)
                    if conn is not self.conn:  # our connection did not last the outage
                        conn.close("ready")
                        kind = 'link'
                    elif not station or int(station[0], 16) != self._station_handle:
                        log.warning("[Flex] Station %s registered again meanwhile, taking our pan: building again",
                                    self.station)
                        kind = 'station'
                    elif self._stream_resumed():
                        log.info("[Flex] DAX-IQ stream 0x%08x resumed by itself", self._stream_id)
                    else:
                        try:
                            self._new_stream(why)
                        except Exception as e:  # no reply: not ready yet, so rebuild after a settle
                            if not self.conn or not self.conn.missed:
                                raise
                            log.warning("[Flex] The radio is not ready (%s): building again", e)
                            kind, settled = 'link', False

                if kind != 'stream':
                    log.error("[Flex] %s: connecting again once the radio is steady", why)
                    if kind == 'station':      # the radio removed them with the station
                        self._stream_id = self._pan_id = self._slice_id = None
                    udp, self._udp = self._udp, None
                    self._close("reconnecting")
                    self._udp = udp
                    end = time.time() + OUTAGE_MAX_S
                    while True:
                        if not settled:            # a steady radio needs no second settle
                            conn = await_radio(self.ip, self.control_port, stopping=stopping, outage=True)
                            if conn is None:
                                self._halt("the radio did not come back within %d min" % (OUTAGE_MAX_S // 60),
                                           alive=False)
                                return
                            conn.close("ready")
                        settled = False
                        try:
                            self.start(why)
                            break
                        except Exception as e:  # no reply: not ready yet, so wait again; a refusal stops
                            if not self._start_missed or self._stopping or time.time() > end:
                                raise
                            log.warning("[Flex] The radio is not ready (%s): waiting again", e)
                self.state = "live"
            except Exception as e:
                self._halt("recovery failed: %s" % e, alive=self.conn is not None and not self.conn.lost)
                return
            log.info("[Flex] Recovered (%s) on attempt %d, %.0f s after it began", kind, self.attempts,
                     time.time() - now)
            log.info("RBN up: %s", time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()))

    def _halt(self, why, alive=True):
        """Leave the radio alone: remove our objects if still connected, disconnect, and stop SparkGap."""
        log.error("[Flex] %s: stopping SparkGap and leaving the radio alone until it is started again", why)
        self.state = "halted"
        self._running = False
        if self.conn and alive:
            self._remove_objects()
        self._stream_id = self._pan_id = self._slice_id = None
        self._close("halted")
        os.kill(os.getpid(), signal.SIGINT)    # SparkGap's normal shutdown

    def health(self):
        """Radio state for the GUI status page."""
        return {'type': 'flex', 'address': self.ip, 'state': self.state,
                'packets': self.packets, 'lost': self.lost, 'reconnects': self.reconnects,
                'last_reconnect': self.last_reconnect, 'station': self.station,
                'daxiq_channel': self.channel, 'others': list(self.others),
                'muted_packets': self.muted_packets,
                'slice': self._slice_id if self.state == "live" else None,  # a reconnect may make another
                'slice_letter': self.slice_letter if self.state == "live" and self._slice_id is not None else None,
                'rx': {**{k: self.rx.get(k) for k in RX_KEYS}, 'ant_list': list(self.rx.get('ant_list', [])),
                       'rfgain_range': list(self.rfgain_range) if self.rfgain_range else None}}

    def retune(self, freq_hz):
        """Move our pan and slice to freq_hz in place: nothing is torn down and no other copy is touched.
        IQ is dropped until RETUNE_SETTLE_S after the move.  A refusal raises once our pan and slice are
        confirmed back where they were; when that cannot be confirmed, SparkGap halts (decoding a frequency
        we do not know is worse than stopping) and it raises."""
        with self._live_lock():
            conn, sl = self.conn, self._slice_id
            if self.state != "live" or not conn or self._pan_id is None or sl is None:
                raise RuntimeError("[Flex] Not live: nothing to retune")
            old, mhz = self.freq_mhz, freq_hz / 1e6
            bw_mhz = self.sample_rate / 1e6
            pan = f"display pan set 0x{self._pan_id:08x} center=%.6f bandwidth={bw_mhz:.6f}"
            self._discard_until = float('inf')
            try:
                if not conn.ok(f"slice {sl} unlocked", conn.cmd(f"slice unlock {sl}")):
                    raise RuntimeError(f"[Flex] The radio did not unlock slice {sl}")
                tune = f"slice tune {sl} %.6f"
                resp = conn.cmd(pan % mhz)
                if conn.ok(f"pan 0x{self._pan_id:08x} center {mhz:.6f} MHz", resp):
                    resp = conn.cmd(tune % mhz)
                    if conn.ok(f"slice {sl} tuned to {mhz:.6f} MHz", resp):
                        self.freq_hz, self.freq_mhz, self.frequencies = freq_hz, mhz, [freq_hz]
                        log.info("[Flex] Retuned to %.6f MHz (%d queued packets of the old frequency dropped)",
                                 mhz, self._drop_queued())
                        return
                    undo = [pan % old] + ([tune % old] if conn.reply(resp) is None else [])
                else:  # a refusal moved nothing; no reply may have
                    undo = [pan % old] if conn.reply(resp) is None else []
                back = all(conn.ok(f"back at {old:.6f} MHz: {c}", conn.cmd(c)) for c in undo)
                if not back:
                    self._halt("retune to %.6f MHz failed and %.6f MHz could not be confirmed" % (mhz, old))
                raise RuntimeError(f"[Flex] The radio refused {mhz:.6f} MHz" + ("" if back else ": halted"))
            finally:
                if self.state != "halted":
                    conn.ok(f"slice {sl} locked", conn.cmd(f"slice lock {sl}"))   # a safety measure, not a guarantee
                    self._discard_until = time.time() + RETUNE_SETTLE_S

    def _drop_queued(self):
        """Empty the DAX-IQ socket: what it holds now came from before the retune, and a backlog can
        outlast RETUNE_SETTLE_S (the receive buffer holds seconds of IQ)."""
        n = 0
        while self._udp is not None:
            try:
                self._udp.recvfrom(DAXIQ_PACKET_SIZE + 128, socket.MSG_DONTWAIT)
            except OSError:
                break
            n += 1
        return n

    def stop(self):
        self._stopping = True
        self._running = False
        time.sleep(0.25)               # receive() leaves its loop; it reads the same connection
        with self._lock:
            self._running = False
            if self.conn:
                self._remove_objects()
            self._close("stopping")
        if self._own_station:
            self._own_station.stop()
            self._own_station = None
        log.info("[Flex] Stopped")

    def _remove_objects(self):
        """Remove our DAX-IQ stream, slice and pan, only while the radio still has them as ours:
        the stream while it comes from the client that made it (ids are reused), the pan while our
        stream feeds it or no stream does (ours, kept by the station after a lost connection)."""
        conn = self.conn
        try:
            if conn and (self._stream_id or self._pan_id):
                status = (conn.cmd("sub daxiq all", settle=0.5) + conn.cmd("sub pan all", settle=0.3)
                          + conn.cmd("sub slice all", settle=0.3))
                streams = daxiq_streams(status)
                ours = streams.get(self._stream_id) if self._stream_id else None
                if ours and ours.get('client') != self._stream_handle:
                    ours = None
                if self._stream_id and not ours:
                    log.info("[Flex] DAX-IQ stream 0x%08x is gone or another client's: leaving it", self._stream_id)
                    self._stream_id = None
                if self._pan_id:
                    fed = bool(ours) and ours.get('pan') == self._pan_id
                    other = any(st.get('pan') == self._pan_id for st in streams.values() if st is not ours)
                    # unfed, it is ours only while the station that owned it still does: ids are reused
                    kept = pan_owners(status).get(self._pan_id) == self._pan_owner
                    if other or not (fed or kept):
                        log.warning("[Flex] Pan 0x%08x is no longer ours (dropped, or another copy's): "
                                    "leaving it and its slice alone", self._pan_id)
                        self._pan_id = self._slice_id = None
                if self._slice_id is not None and not (self._pan_id and self._slice_id in pan_slices(status, self._pan_id)):
                    self._slice_id = None
                if self._stream_id:
                    conn.ok(f"DAX-IQ stream 0x{self._stream_id:08x} removed",
                            conn.cmd(f"stream remove 0x{self._stream_id:08x}", timeout=CMD_TIMEOUT_S))
                if self._slice_id is not None:
                    conn.ok(f"slice {self._slice_id} removed",
                            conn.cmd(f"slice remove {self._slice_id}", timeout=CMD_TIMEOUT_S))
                if self._pan_id:
                    conn.ok(f"pan 0x{self._pan_id:08x} removed",
                            conn.cmd(f"display pan remove 0x{self._pan_id:08x}", timeout=CMD_TIMEOUT_S))
        except Exception:
            log.exception("[Flex] Cleanup failed")
        self._stream_id = self._pan_id = self._slice_id = None

    # -- receive loop --------------------------------------------------------

    def receive(self, callback, duration=None):
        """Receive DAX-IQ data. Calls callback(0, iq_samples) per packet.

        iq_samples is an (n, 2) float64 array of I/Q. Matches HPSDRReceiver.receive();
        rx_index is always 0.
        """
        if not self._running:
            return

        start = time.time()
        last_counter = None
        pkt_count = 0
        lost = 0
        last_report = start
        last_pkt = start
        other_streams = 0
        warned = False

        # Flex v1.4+ sends payload_endian=little. Confirmed empirically:
        # big-endian parses as all zeros, little-endian gives real RF values.
        # AetherSDR's big-endian swap is for older firmware: trust the radio.
        endian_char = '<'

        while self._running:
            if duration and (time.time() - start) >= duration:
                break

            if not pkt_count and not warned and time.time() - start > 10:
                warned = True
                log.warning("[Flex] No DAX-IQ packets after 10 s (%d for other streams): check that "
                            "UDP port %d reaches this machine (WSL mirrored networking, firewall) "
                            "and that DAX-IQ channel %d is bound to the pan",
                            other_streams, self.udp_port, self.channel)
            conn, udp = self.conn, self._udp
            if conn is None or udp is None:
                time.sleep(0.1)
                continue
            ready = select.select([udp, conn], [], [], 0.1)[0]
            why, kind = None, None
            with self._lock:
                if conn is self.conn and conn in ready and self._poll() is None:
                    why, kind = conn.lost, 'link'
            if not why and self._station_lost:
                why, kind = "Station %s left the radio" % self.station, 'station'
            elif not why and pkt_count and time.time() - last_pkt > STALL_S:
                why, kind = "No DAX-IQ packets for %d s (last packet %s)" % (STALL_S, utc_hms(self.last_packet)), 'stream'
            if why:
                self._recover(why, kind)
                last_counter = None
                last_pkt = time.time() + RETRY_S    # time for the new stream to start
                continue
            if udp not in ready:
                continue

            try:
                data, addr = udp.recvfrom(DAXIQ_PACKET_SIZE + 128)
            except socket.error:
                continue

            if len(data) < VITA_HEADER_SIZE + 8:
                continue

            # Parse VITA-49 header
            v0, v1, pkt_words = struct.unpack(">BBH", data[:4])
            stream_id = struct.unpack(">I", data[4:8])[0]

            # Filter: only process our DAX-IQ stream
            if self._stream_id and stream_id != self._stream_id:
                other_streams += 1
                continue

            # Check packet size matches DAX-IQ
            payload_bytes = len(data) - VITA_HEADER_SIZE
            n_samples = payload_bytes // 8
            if n_samples < 1:
                continue

            # Detect packet loss via 4-bit counter
            counter = v1 & 0x0F
            if last_counter is not None:
                gap = (counter - last_counter - 1) & 0x0F
                if gap and pkt_count > 10:
                    lost += gap
                    self.lost += gap
                    log.debug("[Flex] Packet gap: %d missed", gap)   # the 30 s report counts them
            last_counter = counter

            now = last_pkt = self.last_packet = time.time()
            if now < self._discard_until:     # the old frequency, after a retune
                continue

            # Parse float32 I/Q pairs into one (n, 2) float64 array (cdub89/SparkGap#16)
            iq_rows = np.frombuffer(data, dtype=f"{endian_char}f4", count=2 * n_samples,
                                    offset=VITA_HEADER_SIZE).astype(np.float64).reshape(-1, 2)
            iq_rows /= DAXIQ_FULL_SCALE
            if self.transmitting_at(now):
                iq_rows[:] = 0.0   # our own transmission: silence, so the time base holds
                self.muted_packets += 1
            callback(0, iq_rows)

            if not pkt_count:
                log.info("[Flex] OK first DAX-IQ packet after %.1f s (%d samples, stream 0x%08x)",
                         time.time() - start, n_samples, stream_id)
            pkt_count += 1
            self.packets += 1
            if now - last_report >= 30:
                elapsed = now - start
                log.info("[Flex] %d packets in %.0fs (%.0f pkt/s, %d samp/pkt, %d lost)",
                         pkt_count, elapsed, pkt_count / elapsed, n_samples, lost)
                last_report = now


# -- Standalone test ---------------------------------------------------------

if __name__ == '__main__':
    import argparse
    import sys

    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s %(levelname)s %(message)s',
                        datefmt='%H:%M:%S')

    parser = argparse.ArgumentParser(description='Flex DAX-IQ receiver test')
    parser.add_argument('--ip', default='192.168.1.238', help='Flex IP')
    parser.add_argument('--freq', type=float, default=7040000,
                        help='Center frequency (Hz)')
    parser.add_argument('--rate', type=int, default=192000,
                        help='Sample rate (Hz)')
    parser.add_argument('--duration', type=float, default=10,
                        help='Seconds to receive')
    parser.add_argument('--port', type=int, default=7791,
                        help='UDP port')
    args = parser.parse_args()

    rx = FlexIQReceiver(args.ip, freq_hz=int(args.freq),
                        sample_rate=args.rate, udp_port=args.port)

    pkt_count = [0]
    sample_count = [0]
    peak_mag = [0.0]

    def cb(rx_idx, iq):
        pkt_count[0] += 1
        sample_count[0] += len(iq)
        for i_val, q_val in iq[:10]:
            mag = (i_val**2 + q_val**2) ** 0.5
            if mag > peak_mag[0]:
                peak_mag[0] = mag

    rx.start()
    try:
        rx.receive(cb, duration=args.duration)
    except KeyboardInterrupt:
        pass
    finally:
        rx.stop()

    print(f"\nReceived {pkt_count[0]} packets, {sample_count[0]} samples")
    print(f"Peak magnitude: {peak_mag[0]:.6f}")
    rate = sample_count[0] / args.duration if args.duration else 0
    print(f"Effective sample rate: {rate:.0f} Hz")
