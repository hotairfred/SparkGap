#!/usr/bin/env python3
"""
flex_iq.py — FlexRadio 6000 wideband I/Q receiver for OpenSkimmer.

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

    def _cmd(self, cmd, timeout=2.0):
        """Send command and drain response."""
        self._send(cmd)
        time.sleep(0.2)
        return self._drain(timeout)

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        """Connect to the radio, register as GUI, set up DAX-IQ stream."""
        log.info("[Flex] Connecting to %s:%d", self.ip, self.control_port)
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.settimeout(10)
        self._tcp.connect((self.ip, self.control_port))

        # Consume initial handshake (version, handle, status flood)
        self._drain(3)

        # Register as GUI client — required for pan/DAX-IQ creation.
        # AetherSDR showed this is the missing piece for headless operation.
        gui_uuid = str(uuid.uuid4()).upper()
        self._cmd(f"client gui {gui_uuid}")
        self._cmd("client program SmartSDR-Win")
        self._cmd(f"client udpport {self.udp_port}")

        # Create a minimal panadapter (needed as IQ source infrastructure).
        # Use display pan create (no waterfall) to minimize overhead.
        resp = self._cmd(f"display pan create x=1 y=1")
        self._pan_id = self._parse_hex_response(resp)
        if self._pan_id is None:
            # Fallback: try display panafall create
            resp = self._cmd("display panafall create x=1 y=1")
            if resp:
                for line in resp:
                    if line.startswith('R') and '|0|' in line:
                        parts = line.split('|')
                        if len(parts) >= 3:
                            first_id = parts[2].strip().split(',')[0].strip()
                            try:
                                self._pan_id = int(first_id, 16)
                            except ValueError:
                                pass
        if self._pan_id is None:
            log.error("[Flex] Failed to create panadapter")
            return

        log.info("[Flex] Pan created: 0x%08x", self._pan_id)

        # Tune pan to requested frequency
        bw_mhz = self.sample_rate / 1e6
        self._cmd(f"display pan set 0x{self._pan_id:08x} "
                  f"center={self.freq_mhz:.6f} bandwidth={bw_mhz:.6f}")

        # Create a slice to activate the receiver on this frequency.
        # Without an active slice, the Flex streams zeros (no RF path).
        resp = self._cmd(f"slice create freq={self.freq_mhz:.6f} mode=CW")
        self._slice_id = None
        for line in resp:
            if line.startswith('R') and '|0|' in line:
                parts = line.split('|')
                if len(parts) >= 3 and parts[2].strip():
                    try:
                        self._slice_id = int(parts[2].strip())
                    except ValueError:
                        pass
        if self._slice_id is not None:
            log.info("[Flex] Slice created: %d", self._slice_id)

        # Create DAX-IQ stream
        resp = self._cmd(f"stream create type=dax_iq daxiq_channel={self.channel}")
        self._stream_id = self._parse_hex_response(resp)
        if self._stream_id is None:
            log.error("[Flex] Failed to create DAX-IQ stream")
            return
        log.info("[Flex] DAX-IQ stream: 0x%08x", self._stream_id)

        # Set sample rate
        self._cmd(f"stream set 0x{self._stream_id:08x} "
                  f"daxiq_rate={self.sample_rate}")

        # Bind DAX-IQ to the panadapter — THE critical step
        self._cmd(f"display pan set 0x{self._pan_id:08x} daxiq_channel={self.channel}")

        # Open UDP receiver
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self._udp.bind(('', self.udp_port))

        self._running = True
        log.info("[Flex] DAX-IQ streaming at %d Hz on UDP port %d",
                 self.sample_rate, self.udp_port)

    def stop(self):
        self._running = False
        try:
            if self._stream_id:
                self._cmd(f"stream remove 0x{self._stream_id:08x}", timeout=1)
            if self._slice_id is not None:
                self._cmd(f"slice remove {self._slice_id}", timeout=1)
            if self._pan_id:
                self._cmd(f"display pan remove 0x{self._pan_id:08x}", timeout=1)
        except Exception:
            pass
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
        last_report = start

        # Flex v1.4+ sends payload_endian=little. Confirmed empirically:
        # big-endian parses as all zeros, little-endian gives real RF values.
        # AetherSDR's big-endian swap is for older firmware — trust the radio.
        endian_char = '<'

        while self._running:
            if duration and (time.time() - start) >= duration:
                break

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
                    log.debug("[Flex] Packet gap: %d missed", gap)
            last_counter = counter

            # Parse float32 I/Q pairs
            fmt = f"{endian_char}{2 * n_samples}f"
            try:
                samples = struct.unpack(fmt, data[VITA_HEADER_SIZE:
                                                  VITA_HEADER_SIZE + n_samples * 8])
            except struct.error:
                continue

            iq_pairs = list(zip(samples[0::2], samples[1::2]))
            callback(0, iq_pairs)

            pkt_count += 1
            now = time.time()
            if now - last_report >= 30:
                elapsed = now - start
                log.info("[Flex] %d packets in %.0fs (%.0f pkt/s, %d samp/pkt)",
                         pkt_count, elapsed, pkt_count / elapsed, n_samples)
                last_report = now

    # -- helpers -------------------------------------------------------------

    def _parse_hex_response(self, lines):
        """Extract hex ID from an R<seq>|0|<hex_id> response."""
        for line in lines:
            if not line.startswith('R'):
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
