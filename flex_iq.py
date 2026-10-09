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
import re
import select
import socket
import struct
import threading
import time
import uuid

import numpy as np

log = logging.getLogger(__name__)

# VITA-49 DAX-IQ packet layout
VITA_HEADER_SIZE = 28
SAMPLES_PER_PACKET = 512
DAXIQ_PACKET_SIZE = VITA_HEADER_SIZE + SAMPLES_PER_PACKET * 8  # 4124

VALID_RATES = (24000, 48000, 96000, 192000)

# DAX-IQ floats are int16 full scale; FlexLib VitaIFDataPacket divides by 2**15.
DAXIQ_FULL_SCALE = 32768.0

STATION_NAME = 'SparkGap'   # shown in SmartSDR's station list (Multi-Flex)


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
                 daxiq_channel=1, udp_port=7791, control_port=4992):
        if sample_rate not in VALID_RATES:
            raise ValueError(f"sample_rate must be one of {VALID_RATES}")
        self.ip = ip
        self.freq_hz = freq_hz
        self.freq_mhz = freq_hz / 1e6
        self.sample_rate = sample_rate
        self.channel = daxiq_channel
        self.udp_port = udp_port
        self.control_port = control_port
        self.frequencies = [freq_hz]  # match HPSDRReceiver interface

        self._tcp = None
        self._seq = 0
        self._buf = b''
        self._stream_id = None
        self._pan_id = None
        self._slice_id = None

        self._udp = None
        self._running = False

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
        return line

    def _drain(self, timeout=2.0):
        """Read all available lines within timeout, log interesting ones."""
        lines = []
        end = time.time() + timeout
        while time.time() < end:
            line = self._recv_line(timeout=max(0.1, end - time.time()))
            if not line:
                break
            lines.append(line)
            if line.startswith('R') or 'daxiq' in line.lower():
                log.debug("[Flex] <<< %s", line[:200])
        return lines

    def _send(self, cmd):
        self._seq += 1
        line = f"C{self._seq}|{cmd}\n"
        self._tcp.sendall(line.encode())
        log.debug("[Flex] >>> C%d|%s", self._seq, cmd)
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
        log.warning("[Flex] FAILED %s: radio error %s", what, code)
        return False

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        """Connect to the radio, register as GUI, set up DAX-IQ stream."""
        log.info("[Flex] Connecting to %s:%d", self.ip, self.control_port)
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.settimeout(10)
        self._tcp.connect((self.ip, self.control_port))

        # Consume initial handshake (version, handle, status flood)
        hello = self._drain(3)
        handle = next((l[1:].strip().upper() for l in hello if l.startswith('H')), '')
        version = next((l[1:].strip() for l in hello if l.startswith('V')), '?')
        log.info("[Flex] Connected to %s, API %s, client handle 0x%s", self.ip, version, handle)

        # Same order FlexLib uses: program, then gui, then station name.  The
        # name is sent right behind "client gui" without waiting for its reply,
        # so other clients (SmartSDR, SmartStreamer4) see a nameless station
        # for only as long as the radio takes to read the next line.
        # "client gui" is required for pan and DAX-IQ creation (AetherSDR).
        # (A station name sent before "client gui" is refused: R|500000AA.)
        resp = self._cmd(f"client program {STATION_NAME}")
        if any(l.startswith(f"R{self._seq}|10000002|") for l in resp):
            # Not a FlexRadio program name; the radio still records it.
            log.info("[Flex] OK program name %s (radio notes it as an unknown program)", STATION_NAME)
        else:
            self._report("program name " + STATION_NAME, resp)
        self._send(f"client gui {str(uuid.uuid4()).upper()}")
        self._report("station name " + STATION_NAME, self._cmd(f"client station {STATION_NAME}"))
        self._report(f"UDP port {self.udp_port} for DAX-IQ", self._cmd(f"client udpport {self.udp_port}"))

        # The radio gives a new GUI client a default pan with a band-default
        # slice (on a 6600: 14.100 MHz, and a further slice on any pan it
        # creates).  Reuse that pan and slice instead of removing and creating.
        clients = self._cmd("sub client all", settle=0.5)
        for m in re.finditer(r'\|client (0x[0-9A-F]+) connected.*?program=(\S*).*?station=(\S*)',
                             '\n'.join(clients), re.I):
            name = m.group(3).replace('\x7f', ' ')
            if m.group(1).upper() == '0X' + handle:
                log.info("[Flex] Radio lists this client as station '%s' (program %s)", name, m.group(2))
            else:
                log.info("[Flex] Other station on the radio: '%s' (program %s, client %s)",
                         name, m.group(2), m.group(1))
        status = self._cmd("sub pan all", settle=0.5) + self._cmd("sub slice all", settle=0.5)
        slices, pans = owned_objects(status, handle)
        if not pans:
            status += self._drain(1.0)     # the default pan's status can lag
            slices, pans = owned_objects(status, handle)
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
            slices, _ = owned_objects(self._cmd("sub slice all", settle=0.5), handle)
        self._report(f"pan 0x{self._pan_id:08x} center {self.freq_mhz:.6f} MHz, bandwidth {bw_mhz:.3f} MHz",
                     self._cmd(f"display pan set 0x{self._pan_id:08x} "
                               f"center={self.freq_mhz:.6f} bandwidth={bw_mhz:.6f}"))

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

        self._running = True
        log.info("[Flex] DAX-IQ streaming at %d Hz on UDP port %d",
                 self.sample_rate, self.udp_port)
        log.info("[Flex] Ready: station %s (client 0x%s), pan 0x%08x at %.6f MHz x %.3f MHz, "
                 "slice %s CW, DAX-IQ channel %d stream 0x%08x at %d Hz to UDP %d",
                 STATION_NAME, handle, self._pan_id, self.freq_mhz, bw_mhz, self._slice_id,
                 self.channel, self._stream_id, self.sample_rate, self.udp_port)

    def stop(self):
        self._running = False
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
            log.exception("[Flex] Cleanup on stop failed")
        if self._tcp:
            try:
                self._tcp.close()
            except Exception:
                pass
            self._tcp = None
        if self._udp:
            try:
                self._udp.close()
            except Exception:
                pass
            self._udp = None
        log.info("[Flex] Stopped")

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
            ready = select.select([self._udp], [], [], 0.1)
            if not ready[0]:
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
                    log.debug("[Flex] Packet gap: %d missed", gap)
            last_counter = counter

            # Parse float32 I/Q pairs
            fmt = f"{endian_char}{2 * n_samples}f"
            try:
                samples = struct.unpack(fmt, data[VITA_HEADER_SIZE:
                                                  VITA_HEADER_SIZE + n_samples * 8])
            except struct.error:
                continue

            iq_pairs = [(i / DAXIQ_FULL_SCALE, q / DAXIQ_FULL_SCALE)
                        for i, q in zip(samples[0::2], samples[1::2])]
            callback(0, iq_pairs)

            if not pkt_count:
                log.info("[Flex] OK first DAX-IQ packet after %.1f s (%d samples, stream 0x%08x)",
                         time.time() - start, n_samples, stream_id)
            pkt_count += 1
            now = time.time()
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
