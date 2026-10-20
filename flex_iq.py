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

Protocol refs:
  - AetherSDR source (ten9876/AetherSDR) — RadioModel.cpp, DaxIqModel.cpp
  - AB4EJ-1/FlexRadioIQ — VITA-49 packet struct
  - SmartSDR TCP/IP API wiki
"""

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
STALL_S = 10                # no DAX-IQ packet for this long: set the radio up again
RETRY_S = 30                # after a reconnect: time for the new stream to start
HALT_WINDOW_S = 600         # a second loss within this long stops SparkGap: no reconnect loops on the radio


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


def slice_pans(lines, handle):
    """Owned slice number -> the pan it sits on, from slice status lines."""
    out = {}
    own = re.compile(r'client_handle=0x' + re.escape(handle) + r'\b', re.I) if handle else None
    for line in lines:
        m = re.search(r'\|slice (\d+) .*\bpan=(0x[0-9A-F]+)', line, re.I)
        if m and own and own.search(line) and 'in_use=0' not in line:
            out[int(m.group(1))] = m.group(2).lower()
    return out


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


def radio_nickname(lines):
    """The radio's nickname from its reply to "info" (name="Dallas"), or '' when it has none."""
    for line in lines:
        m = re.match(r'R\d+\|0\|.*?\bname="([^"]*)"', line)
        if m:
            return m.group(1).strip()
    return ''


def station_for(nickname, channel):
    """SparkGap-<radio nickname>, one station for every copy on that radio;
    SparkGap-<DAX-IQ channel> when the radio has no nickname."""
    nick = re.sub(r'[^A-Za-z0-9_-]+', '-', nickname).strip('-')
    return f"{STATION_NAME}-{nick or channel}"


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


class FlexIQReceiver:
    """DAX-IQ receiver for a FlexRadio 6000/8000 series.

    Drop-in replacement for HPSDRReceiver — same callback contract:
        callback(rx_index, iq_samples)
    where iq_samples is list[(float_i, float_q)].

    Usage:
        rx = FlexIQReceiver('192.168.1.238', freq_hz=7040000,
                            sample_rate=192000)
        rx.start()
        rx.receive(callback)   # blocks
        rx.stop()
    """

    def __init__(self, ip, freq_hz=7040000, sample_rate=192000,
                 daxiq_channel=1, udp_port=7791, control_port=4992, station=None,
                 rxant=None, loopa=None, loopb=None):
        if sample_rate not in VALID_RATES:
            raise ValueError(f"sample_rate must be one of {VALID_RATES}")
        self.ip = ip
        self.freq_hz = freq_hz
        self.freq_mhz = freq_hz / 1e6
        self.sample_rate = sample_rate
        self.channel = daxiq_channel
        self._station_cfg = station  # flex_station; else SparkGap-<radio nickname>, set on connect
        # flex_rxant / flex_loopa / flex_loopb: set on our pan and slice; None leaves the radio's
        # (band-persistence) value.  A receive loop is shared by every pan on that antenna, so a
        # pan that comes up with the loop off turns it off for whatever hangs on the loop.
        self.rx_path = ''.join(f' {k}={v}' for k, v in (('rxant', rxant),
                                                       ('loopa', None if loopa is None else int(bool(loopa))),
                                                       ('loopb', None if loopb is None else int(bool(loopb))))
                               if v is not None)
        self.station = station or f"{STATION_NAME}-{daxiq_channel}"
        self.client_id = ''
        self.udp_port = udp_port
        self.control_port = control_port
        self.frequencies = [freq_hz]  # match HPSDRReceiver interface

        self._tcp = None
        self._handle = ''          # client handle of the open connection, for the disconnect line
        self._lost = None          # why _read_status found the connection gone
        self.last_packet = None    # time.time() of the newest DAX-IQ packet since the last connect
        self._seq = 0
        self._sent = {}            # seq -> command, until its reply arrives
        self._buf = b''
        self._stream_id = None
        self._pan_id = None
        self._slice_id = None

        self._udp = None
        self._running = False

        # for the GUI status page (health())
        self.state = "connecting"
        self.packets = 0
        self.lost = 0
        self.reconnects = 0
        self.last_reconnect = None
        self.attempts = 0         # connection attempts, first start and reconnects
        self._last_loss = None    # time.time() of the last lost connection
        self._bound = False       # attached to another copy's station with client bind
        self._stale = None        # (stream, slice, pan) a bound copy left when its connection was lost
        self._stopping = False
        self._lock = threading.Lock()   # stop() waits for a reconnect in progress
        self._ptt_warned = False
        self.others = []          # other stations on the radio at the last connect

    # -- TCP control channel -------------------------------------------------

    def _recv_line(self, timeout=3.0):
        self._tcp.settimeout(timeout)
        while b'\n' not in self._buf:
            try:
                chunk = self._tcp.recv(4096)
            except socket.timeout:
                return ''
            if not chunk:
                return ''
            self._buf += chunk
        idx = self._buf.index(b'\n')
        line = self._buf[:idx].decode('utf-8', errors='replace').strip()
        self._buf = self._buf[idx + 1:]
        self._log_reply(line)
        return line

    def _log_reply(self, line):
        r = reply_log(line, self._sent)
        if r:
            log.log(*r)

    def _drain(self, timeout=2.0):
        """Read all available lines within timeout, log interesting ones."""
        lines = []
        end = time.time() + timeout
        while time.time() < end:
            line = self._recv_line(timeout=max(0.1, end - time.time()))
            if not line:
                break
            lines.append(line)
            if 'daxiq' in line.lower():
                log.debug("[Flex] <<< %s", line[:200])
        return lines

    def _send(self, cmd):
        self._seq += 1
        line = f"C{self._seq}|{cmd}\n"
        self._sent[self._seq] = cmd
        self._tcp.sendall(line.encode())
        log.info("[Flex] >>> C%d|%s", self._seq, cmd)
        return self._seq

    def _cmd(self, cmd, timeout=2.0, settle=0.0):
        """Send a command and read until its own reply R<seq>|... (or timeout),
        then keep reading `settle` seconds for status lines that follow it."""
        seq = self._send(cmd)
        lines = []
        end = time.time() + timeout
        while time.time() < end:
            line = self._recv_line(timeout=max(0.05, end - time.time()))
            if not line:
                break
            lines.append(line)
            if line.startswith(f"R{seq}|"):
                break
        if settle:
            lines += self._drain(settle)
        return lines

    def _report(self, what, resp):
        """Log OK or FAILED (with the radio's error code) for the last command."""
        reply = next((l for l in resp if l.startswith(f"R{self._seq}|")), None)
        if reply is None:
            log.warning("[Flex] FAILED %s: no reply from the radio", what)
            return False
        code = reply.split('|')[1]
        if code == '0':
            log.info("[Flex] OK %s", what)
            return True
        log.info("[Flex] FAILED %s: radio error %s", what, code)   # the reply line above is the warning
        return False

    # -- lifecycle -----------------------------------------------------------

    def start(self, why=None):
        """Connect to the radio, register as GUI, set up DAX-IQ stream.  `why`: the reason for a reconnect."""
        self._stream_id = self._pan_id = self._slice_id = None
        self.last_packet = None
        self._sent = {}
        self._handle = ''
        self._ptt_warned = False
        self.attempts += 1
        log.info("[Flex] Connection attempt %d to %s:%d (%s)", self.attempts, self.ip, self.control_port,
                 "reconnect: %s" % why if why else "first start")
        try:
            self._setup()
        except Exception as e:
            log.error("[Flex] Connection attempt %d failed: %s", self.attempts, e)
            # a copy that joined another station leaves its pan to that station,
            # which the radio keeps after we disconnect (2026-10-18)
            self._remove_objects()
            self._close("failed start")
            raise

    def _setup(self):
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.settimeout(10)
        self._tcp.connect((self.ip, self.control_port))

        # Consume initial handshake (version, handle, status flood)
        hello = self._drain(3)
        handle = next((l[1:].strip().upper() for l in hello if l.startswith('H')), '')
        version = next((l[1:].strip() for l in hello if l.startswith('V')), '?')
        log.info("[Flex] Connected to %s, API %s, client handle 0x%s", self.ip, version, handle)
        self._handle = handle

        # One station per radio: every copy on it names the same station, so a later copy
        # finds the first one's GUI client below and attaches to it.
        if not self._station_cfg:
            nickname = radio_nickname(self._cmd("info"))
            self.station = station_for(nickname, self.channel)
            log.info("[Flex] Station %s (radio nickname %s)", self.station, repr(nickname) if nickname else "none")
        # one GUI client ID per radio and station, the same on every start, as FlexLib keeps its own
        self.client_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"sparkgap://{self.ip}/{self.station}")).upper()

        # Same order FlexLib uses: program, then gui, then station name.  The
        # name waits for the reply to "client gui": sent before the radio has
        # registered the GUI client it is refused (R|500000AA), which happened
        # to a second copy on a radio that already had one.
        # "client gui" is required for pan and DAX-IQ creation (AetherSDR).
        # The radio takes two GUI clients (SmartSDR is usually one).  A copy it
        # refuses attaches to the SparkGap station already there (FlexLib's
        # "client bind"), so its pan and slice belong to that station.
        resp = self._cmd(f"client program {STATION_NAME}")
        if any(l.startswith(f"R{self._seq}|10000002|") for l in resp):
            # Not a FlexRadio program name; the radio still records it.
            log.info("[Flex] OK program name %s (radio notes it as an unknown program)", STATION_NAME)
        else:
            self._report("program name " + STATION_NAME, resp)
        # As FlexLib, pick the role first: a station that already has its GUI client
        # is joined with "client bind"; "client gui" with the station's ID evicts it.
        joined = [g for g in gui_stations(self._cmd("sub client all", settle=0.5))
                  if g[2] == STATION_NAME and g[3] == self.station]
        self._bound = False
        if joined:
            self._bound = self._report(f"attached to station {self.station}",
                                       self._cmd(f"client bind client_id={joined[0][1]}"))
        elif self._report("GUI client", self._cmd(f"client gui {self.client_id}", timeout=5.0)):
            self._report("station name " + self.station, self._cmd(f"client station {self.station}"))
        else:
            listed = self._cmd("sub client all", settle=0.5)
            ours = [g for g in gui_stations(listed) if g[2] == STATION_NAME]
            if not ours:
                log.warning("[Flex] Client status from the radio: %s", [l for l in listed if 'client' in l])
                raise RuntimeError("[Flex] No free GUI client on the radio and no SparkGap station to attach to")
            _, client_id, _, self.station = ours[0]
            self._bound = self._report(f"attached to station {self.station}",
                                       self._cmd(f"client bind client_id={client_id}"))
        self._report(f"UDP port {self.udp_port} for DAX-IQ", self._cmd(f"client udpport {self.udp_port}"))
        if self._stale and self._bound:    # the station kept them when our connection was lost
            self._stream_id, self._slice_id, self._pan_id = self._stale
            self._remove_objects()
            self._stream_id = self._pan_id = self._slice_id = None
        self._stale = None

        # The radio gives a new GUI client a default pan with a band-default
        # slice (on a 6600: 14.100 MHz, and a further slice on any pan it
        # creates).  Reuse that pan and slice instead of removing and creating.
        clients = self._cmd("sub client all", settle=0.5)
        self.others = []
        for m in re.finditer(r'\|client (0x[0-9A-F]+) connected.*?program=(\S*).*?station=(\S*)',
                             '\n'.join(clients), re.I):
            name = m.group(3).replace('\x7f', ' ')
            if m.group(1).upper() == '0X' + handle:
                log.info("[Flex] Radio lists this client as station '%s' (program %s)", name, m.group(2))
            else:
                log.info("[Flex] Other station on the radio: '%s' (program %s, client %s)",
                         name, m.group(2), m.group(1))
                self.others.append(f"{name} ({m.group(2)})")
        status = self._cmd("sub pan all", settle=0.5) + self._cmd("sub slice all", settle=0.5)
        slices, pans = owned_objects(status, handle)
        if not pans:
            status += self._drain(1.0)     # the default pan's status can lag
            slices, pans = owned_objects(status, handle)
        # A DAX-IQ channel another station's pan holds (Maestro, SmartSDR, DAX) is not ours to take.
        stations = gui_stations(clients)
        num = lambda h: int(h, 16) if h else None   # handles compare as numbers, not text
        ours = {num(handle)}    # a pan of our station on this channel is another copy's, or left by a crash
        taken = {p: h for p, h in daxiq_holders(status, self.channel).items() if num(h) not in ours}
        if taken:
            pan, h = sorted(taken.items())[0]
            who = next((f"{g[3]} ({g[2]})" for g in stations if num(g[0]) == num(h)), f"client {h}")
            raise RuntimeError(f"[Flex] DAX-IQ {self.channel} is in use by {who} on pan {pan}; "
                               "set another flex_daxiq_channel or free it there")
        bw_mhz = self.sample_rate / 1e6
        if pans:
            on_pan = slice_pans(status, handle)
            keep = next((p for p in sorted(pans) if p.lower() in on_pan.values()), sorted(pans)[0])
            self._pan_id = int(keep, 16)
            slices = {n for n, p in on_pan.items() if p == keep.lower()} or slices
            for p in sorted(pans - {keep}):
                self._cmd(f"display pan remove {p}")
            log.info("[Flex] Using the radio's default pan 0x%08x", self._pan_id)
        else:
            resp = self._cmd("display pan create x=1 y=1")
            self._pan_id = self._parse_hex_response(resp)
            if self._pan_id is None:
                raise RuntimeError("[Flex] No free panadapter on the radio; close one in SmartSDR or on the front panel")
            log.info("[Flex] Pan created: 0x%08x", self._pan_id)
            status = self._cmd("sub slice all", settle=0.5)
            slices, _ = owned_objects(status, handle)
            if self._bound:                # the station owns the slice the radio put on our pan
                slices = pan_slices(status, self._pan_id)
        self._report(f"pan 0x{self._pan_id:08x} center {self.freq_mhz:.6f} MHz, bandwidth {bw_mhz:.3f} MHz",
                     self._cmd(f"display pan set 0x{self._pan_id:08x} "
                               f"center={self.freq_mhz:.6f} bandwidth={bw_mhz:.6f}{self.rx_path}"))

        # One slice keeps the RF path live (without one the Flex streams zeros).
        # dax=0: the default slice comes with a DAX audio channel; we need none.
        self._slice_id = min(slices) if slices else None
        if self._slice_id is not None:
            self._report(f"slice {self._slice_id} tuned to {self.freq_mhz:.6f} MHz",
                         self._cmd(f"slice tune {self._slice_id} {self.freq_mhz:.6f}"))
            self._report(f"slice {self._slice_id} mode CW",          # one field per command,
                         self._cmd(f"slice set {self._slice_id} mode=CW"))   # as FlexLib sends them
            self._report(f"slice {self._slice_id} DAX audio off",
                         self._cmd(f"slice set {self._slice_id} dax=0"))
            log.info("[Flex] Using the radio's default slice %d at %.6f MHz",
                     self._slice_id, self.freq_mhz)
        else:
            resp = self._cmd(f"slice create pan=0x{self._pan_id:08x} freq={self.freq_mhz:.6f} mode=CW")
            raw = next((l.split('|')[2].strip() for l in resp
                        if l.startswith(f"R{self._seq}|0|") and len(l.split('|')) >= 3), '')
            self._slice_id = int(raw) if raw.isdigit() else None
            if self._slice_id is None:
                log.warning("[Flex] Slice create reply not understood: %s", resp[-3:])
            else:
                log.info("[Flex] Slice created: %d", self._slice_id)
        if self._slice_id is not None:     # unmuted, the operator hears it in the radio's headphones
            self._report(f"slice {self._slice_id} muted",
                         self._cmd(f"slice set {self._slice_id} audio_mute=1"))
            self._report(f"slice {self._slice_id} not a transmit slice",   # the key must never land on it
                         self._cmd(f"slice set {self._slice_id} tx=0"))
            if self.rx_path:                  # the slice carries its own copy of the loop settings
                self._report(f"slice {self._slice_id}{self.rx_path}",
                             self._cmd(f"slice set {self._slice_id}{self.rx_path}"))
        all_slices, _ = owned_objects(self._cmd("sub slice all", settle=0.3), handle)
        extra = all_slices - {self._slice_id} if self._slice_id is not None else set()
        for n in sorted(extra):
            self._cmd(f"slice remove {n}")
        if extra:
            log.info("[Flex] Freed extra slices %s", sorted(extra))

        # Create DAX-IQ stream
        resp = self._cmd(f"stream create type=dax_iq daxiq_channel={self.channel}")
        self._stream_id = self._parse_hex_response(resp)
        if self._stream_id is None:
            raise RuntimeError("[Flex] Failed to create DAX-IQ stream")
        log.info("[Flex] DAX-IQ stream: 0x%08x", self._stream_id)

        # Bind DAX-IQ to the panadapter — THE critical step
        self._report(f"DAX-IQ channel {self.channel} bound to pan 0x{self._pan_id:08x}",
                     self._cmd(f"display pan set 0x{self._pan_id:08x} daxiq_channel={self.channel}"))

        # Set sample rate after binding; newer firmware rejects it on an unbound stream
        resp = self._cmd(f"stream set 0x{self._stream_id:08x} "
                         f"daxiq_rate={self.sample_rate}")
        if not any(l.startswith(f"R{self._seq}|0|") for l in resp):
            raise RuntimeError(f"[Flex] Radio refused daxiq_rate={self.sample_rate}: {resp[-1:]}")

        # Open UDP receiver
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self._udp.bind(('', self.udp_port))

        self._running = not self._stopping
        log.info("[Flex] DAX-IQ streaming at %d Hz on UDP port %d",
                 self.sample_rate, self.udp_port)
        self.state = "live"
        log.info("[Flex] Ready: station %s (client 0x%s), pan 0x%08x at %.6f MHz x %.3f MHz, "
                 "slice %s CW, DAX-IQ channel %d stream 0x%08x at %d Hz to UDP %d",
                 self.station, handle, self._pan_id, self.freq_mhz, bw_mhz, self._slice_id,
                 self.channel, self._stream_id, self.sample_rate, self.udp_port)

    def _close(self, reason):
        if self._tcp:
            try:
                self._tcp.sendall(b'\x04')    # FlexLib tells the radio it is leaving before it closes
            except OSError:
                pass
        if self._tcp and self._handle:
            log.info("[Flex] Disconnected from %s (client 0x%s, %s)", self.ip, self._handle, reason)
        for name in ('_tcp', '_udp'):
            sock = getattr(self, name)
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
                setattr(self, name, None)

    def _read_status(self):
        """Read what the radio has sent on the control connection.  Unread, its
        status messages (many while the operator transmits) fill the socket
        buffer and the radio drops the station: 2026-10-13, 14 h into a run.
        Returns False when the connection is gone, with the cause in self._lost."""
        try:
            chunk = self._tcp.recv(65536, socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError, TimeoutError):
            return True
        except OSError as e:
            self._lost = "Control connection error: %s" % e
            return False
        if not chunk:
            self._lost = "The radio closed the control connection"
            return False
        *lines, self._buf = (self._buf + chunk).split(b'\n')
        lines = [l.decode('utf-8', errors='replace') for l in lines]
        for line in lines:
            self._log_reply(line)
        for line in clients_gone(lines):
            log.info("[Flex] Radio reports: %s", line.strip()[:200])
        if ptt_ours(lines) and not self._ptt_warned:
            self._ptt_warned = True
            log.warning("[Flex] The radio's key and PTT jacks now follow the SparkGap station, "
                        "not the operator's: set local PTT in SmartSDR")
        return True

    def _restart(self, why, alive):
        """Drop the connection and set the radio up once more; a second loss within
        HALT_WINDOW_S, or a failed reconnect, stops SparkGap instead.
        alive: the control connection still works (the stream stalled)."""
        with self._lock:
            if self._stopping:
                return
            now = time.time()
            if self._last_loss and now - self._last_loss < HALT_WINDOW_S:
                self._halt("%s, %.0f s after the previous lost connection" % (why, now - self._last_loss), alive)
                return
            self._last_loss = now
            log.error("[Flex] %s: connecting again", why)
            self.state = "reconnecting"
            self.reconnects += 1
            self.last_reconnect = round(now)
            if alive:
                self._remove_objects()
            elif self._bound:
                self._stale = (self._stream_id, self._slice_id, self._pan_id)
            self._close("reconnecting")
            self._buf = b''
            try:
                self.start(why)
            except Exception:
                self._halt("reconnect attempt %d failed" % self.attempts)
                return
            log.info("[Flex] Reconnected on attempt %d, %.0f s after reconnecting began",
                     self.attempts, time.time() - now)

    def _halt(self, why, alive=True):
        """Leave the radio alone: remove our objects if still connected, disconnect, and stop SparkGap."""
        log.error("[Flex] %s: stopping SparkGap and leaving the radio alone until it is started again", why)
        self.state = "halted"
        self._running = False
        if self._tcp and alive:
            self._remove_objects()
        self._stream_id = self._pan_id = self._slice_id = None
        self._close("halted")
        os.kill(os.getpid(), signal.SIGINT)    # SparkGap's normal shutdown

    def health(self):
        """Radio state for the GUI status page."""
        return {'type': 'flex', 'address': self.ip, 'state': self.state,
                'packets': self.packets, 'lost': self.lost, 'reconnects': self.reconnects,
                'last_reconnect': self.last_reconnect, 'station': self.station,
                'daxiq_channel': self.channel, 'others': list(self.others)}

    def stop(self):
        self._stopping = True
        self._running = False
        time.sleep(0.25)               # receive() leaves its loop; it reads the same connection
        with self._lock:
            self._running = False
            self._remove_objects()
            self._close("stopping")
        log.info("[Flex] Stopped")

    def _remove_objects(self):
        """Remove our DAX-IQ stream, slice and pan from the radio."""
        try:
            if self._stream_id:
                self._report(f"DAX-IQ stream 0x{self._stream_id:08x} removed",
                             self._cmd(f"stream remove 0x{self._stream_id:08x}", timeout=1))
            if self._slice_id is not None:
                self._report(f"slice {self._slice_id} removed",
                             self._cmd(f"slice remove {self._slice_id}", timeout=1))
            if self._pan_id:
                self._report(f"pan 0x{self._pan_id:08x} removed",
                             self._cmd(f"display pan remove 0x{self._pan_id:08x}", timeout=1))
        except Exception:
            log.exception("[Flex] Cleanup failed")

    # -- receive loop --------------------------------------------------------

    def receive(self, callback, duration=None):
        """Receive DAX-IQ data. Calls callback(0, iq_samples) per packet.

        iq_samples is a list of (float_i, float_q) tuples. The Flex
        emits float32 natively — no int24→float conversion needed.

        Matches HPSDRReceiver.receive() contract. rx_index is always 0.
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
        # AetherSDR's big-endian swap is for older firmware — trust the radio.
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
            ready = select.select([self._udp, self._tcp], [], [], 0.1)[0]
            why, alive = None, True
            if self._tcp in ready and not self._read_status():
                why, alive = self._lost, False
            elif pkt_count and time.time() - last_pkt > STALL_S:
                why = "No DAX-IQ packets for %d s (last packet %s)" % (STALL_S, utc_hms(self.last_packet))
            if why:
                self._restart(why, alive)
                last_counter = None
                last_pkt = time.time() + RETRY_S    # time for the new stream to start
                continue
            if self._udp not in ready:
                continue

            try:
                data, addr = self._udp.recvfrom(DAXIQ_PACKET_SIZE + 128)
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

            # Parse float32 I/Q pairs into one (n, 2) float64 array (cdub89/SparkGap#16)
            iq_rows = np.frombuffer(data, dtype=f"{endian_char}f4", count=2 * n_samples,
                                    offset=VITA_HEADER_SIZE).astype(np.float64).reshape(-1, 2)
            iq_rows /= DAXIQ_FULL_SCALE
            callback(0, iq_rows)

            if not pkt_count:
                log.info("[Flex] OK first DAX-IQ packet after %.1f s (%d samples, stream 0x%08x)",
                         time.time() - start, n_samples, stream_id)
            pkt_count += 1
            self.packets += 1
            now = last_pkt = self.last_packet = time.time()
            if now - last_report >= 30:
                elapsed = now - start
                log.info("[Flex] %d packets in %.0fs (%.0f pkt/s, %d samp/pkt, %d lost)",
                         pkt_count, elapsed, pkt_count / elapsed, n_samples, lost)
                last_report = now

    # -- helpers -------------------------------------------------------------

    def _parse_hex_response(self, lines):
        """Extract hex ID from the R<seq>|0|<hex_id> reply to the last command."""
        for line in lines:
            if not line.startswith(f"R{self._seq}|"):
                continue
            parts = line.split('|')
            if len(parts) >= 3 and parts[1] == '0' and parts[2].strip():
                raw = parts[2].strip().split(',')[0].strip()
                try:
                    return int(raw, 16)
                except ValueError:
                    try:
                        return int(raw)
                    except ValueError:
                        pass
        return None


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
