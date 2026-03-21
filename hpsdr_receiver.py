#!/usr/bin/env python3
"""
hpsdr_receiver.py — HPSDR Protocol 1 IQ receiver for Red Pitaya.

Connects to a Red Pitaya running sdr_receiver_hpsdr, configures receivers
on specified frequencies, and outputs raw IQ data to stdout or files.

Protocol reference: openHPSDR Protocol 1 (Metis/Hermes compatible)
Pitaya app: Pavel Demin's sdr_receiver_hpsdr

Usage:
    # Discover devices
    python3 hpsdr_receiver.py --discover

    # Receive 8 bands, output IQ to files
    python3 hpsdr_receiver.py --ip 192.168.1.54 --bands 3500,7000,10100,14000,18068,21000,24890,28000

    # Single band to stdout (pipe to decoder)
    python3 hpsdr_receiver.py --ip 192.168.1.54 --freq 7000000 --stdout

    # Record to WAV files
    python3 hpsdr_receiver.py --ip 192.168.1.54 --bands 7000,14000 --wav --duration 60
"""

import socket
import select
import struct
import sys
import os
import time
import wave
import argparse
import signal
import numpy as np

# HPSDR Protocol 1 constants
HPSDR_PORT = 1024
COOKIE = b'\xef\xfe'
METIS_DISCOVERY = 0x02
METIS_START = 0x04
METIS_STOP = 0x04

# Sample rate is fixed at 48000 for Protocol 1 with 1 receiver
# With multiple receivers, the per-receiver rate drops:
# 1 rx = 48000, 2 rx = 48000, 3 rx = 48000, 4 rx = 48000 (384kHz ADC / 8)
# Pavel Demin's sdr_receiver_hpsdr uses 48kHz per receiver
SAMPLE_RATE = 48000

# Default band frequencies (kHz) for 8-band skimmer
DEFAULT_BANDS = [3500, 7000, 10100, 14000, 18068, 21000, 24890, 28000]

# Running flag for clean shutdown
running = True


def signal_handler(sig, frame):
    global running
    running = False
    print("\nShutting down...", file=sys.stderr)


def discover(interface=None, port=HPSDR_PORT, timeout=2.0):
    """Discover HPSDR devices on the network."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)

    if interface:
        sock.bind((interface, port))

    # Discovery packet: 0xEF 0xFE 0x02 + 57 zeros
    msg = bytes([0xEF, 0xFE, 0x02]) + bytes(57)
    sock.sendto(msg, ('255.255.255.255', port))

    devices = []
    deadline = time.time() + timeout

    while time.time() < deadline:
        ready = select.select([sock], [], [], 0.5)
        if ready[0]:
            data, addr = sock.recvfrom(1500)
            if len(data) >= 11 and data[0:2] == COOKIE:
                mac = ':'.join(f'{b:02x}' for b in data[3:9])
                status = data[2]
                board_id = data[10] if len(data) > 10 else 0
                gw_ver = data[9] if len(data) > 9 else 0
                n_rx = data[0x13] if len(data) > 0x13 else 1
                devices.append({
                    'ip': addr[0],
                    'port': addr[1],
                    'mac': mac,
                    'status': status,
                    'board_id': board_id,
                    'gateware': gw_ver,
                    'receivers': n_rx,
                })

    sock.close()
    return devices


def build_c0_packet(receivers=8, duplex=True, speed=0):
    """Build C0 configuration register (sent in EP2 USB frames).

    C0[0] = MOX bit (0 = RX)
    C0[1-4] = Configuration:
      C1: speed (00=48k), 10MHz ref, 122.88MHz source, mic source
      C2: sample mode, class E, OpenCollector
      C3: Alex filters, attenuator
      C4: duplex, #receivers-1
    """
    c0 = bytes([0x00])  # No MOX

    # C1: speed=0 (48kHz), internal 10MHz, Mercury 122.88MHz
    c1 = (speed & 0x03)  # bits 1:0 = speed
    c1_bytes = bytes([0x00, c1, 0x00, 0x00])

    # C2-C4: duplex mode, N receivers
    # C0[0] bit 7 = 0 means these are C0-C4 registers
    # Different C0 addresses carry different register sets
    # We need to cycle through the register addresses

    return c0, c1_bytes


def build_freq_packet(rx_index, freq_hz):
    """Build frequency setting packet for receiver rx_index.

    C0 byte indicates which receiver: 0x02=RX1, 0x04=RX2, etc.
    C1-C4 = frequency in Hz (big-endian 32-bit)
    """
    # RX1=0x02, RX2=0x04, RX3=0x06, ... RX8=0x10
    c0 = bytes([(rx_index + 1) * 2])
    freq_bytes = struct.pack('>I', int(freq_hz))
    return c0 + freq_bytes


def build_ep2_frame(c0_c4, iq_samples=None):
    """Build an EP2 USB frame (512 bytes).

    Structure: 3 sync bytes + 5 C&C bytes + 504 bytes audio/zeros
    Two frames per 1032-byte UDP packet.
    """
    sync = bytes([0x7F, 0x7F, 0x7F])

    if len(c0_c4) < 5:
        c0_c4 = c0_c4 + bytes(5 - len(c0_c4))

    # 504 bytes of audio data (zeros for RX-only)
    if iq_samples is None:
        audio = bytes(504)
    else:
        audio = iq_samples[:504]
        if len(audio) < 504:
            audio += bytes(504 - len(audio))

    return sync + c0_c4 + audio


def build_udp_packet(seq, frame1_c0c4, frame2_c0c4):
    """Build a 1032-byte UDP packet with two EP2 frames.

    Header: 0xEF 0xFE 0x01 0x02 + 4-byte sequence number
    Followed by two 512-byte EP2 frames
    """
    header = bytes([0xEF, 0xFE, 0x01, 0x02]) + struct.pack('>I', seq)
    f1 = build_ep2_frame(frame1_c0c4)
    f2 = build_ep2_frame(frame2_c0c4)
    return header + f1 + f2


def parse_iq_packet(data, n_receivers=8):
    """Parse a received IQ data packet.

    Returns list of (receiver_index, [(i, q), ...]) tuples.
    Each IQ sample is 24-bit signed (3 bytes I, 3 bytes Q).
    With 8 receivers: each frame has 63 sets of 8 IQ pairs = 504 IQ samples.
    """
    if len(data) < 1032:
        return None

    # Check header
    if data[0:2] != COOKIE or data[2] != 0x01 or data[3] != 0x06:
        return None

    seq = struct.unpack('>I', data[4:8])[0]
    samples_per_rx = {1: 63, 2: 36, 3: 25, 4: 19, 5: 15, 6: 13, 7: 11, 8: 10}
    n_samples = samples_per_rx.get(n_receivers, 10)

    # Each frame: 3 sync + 5 C&C + IQ data
    # IQ data: repeating groups of (n_receivers * 6 bytes) + 2 bytes mic
    # Each IQ sample: 3 bytes I (24-bit signed) + 3 bytes Q (24-bit signed)
    all_iq = [[] for _ in range(n_receivers)]

    for frame_offset in [8, 520]:  # Two 512-byte frames per packet
        pos = frame_offset + 8  # Skip sync + C&C

        bytes_per_group = n_receivers * 6 + 2  # +2 for mic sample
        remaining = 504
        while remaining >= bytes_per_group:
            for rx in range(n_receivers):
                # 24-bit signed I
                i_bytes = data[pos:pos + 3]
                i_val = int.from_bytes(i_bytes, 'big', signed=True)
                pos += 3
                # 24-bit signed Q
                q_bytes = data[pos:pos + 3]
                q_val = int.from_bytes(q_bytes, 'big', signed=True)
                pos += 3
                all_iq[rx].append((i_val / 8388608.0, q_val / 8388608.0))

            # Skip 2-byte mic sample
            pos += 2
            remaining -= bytes_per_group

    return seq, all_iq


class HPSDRReceiver:
    """HPSDR Protocol 1 receiver for Red Pitaya."""

    def __init__(self, ip, port=HPSDR_PORT, n_receivers=8):
        self.ip = ip
        self.port = port
        self.n_receivers = n_receivers
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        self.sock.bind(('', port))
        self.sock.setblocking(False)
        self.seq = 0
        self.frequencies = [7000000] * n_receivers  # Default 40m

    def set_frequency(self, rx_index, freq_hz):
        """Set frequency for a receiver (in Hz).

        Applies -3.9 ppm frequency calibration for the Red Pitaya STEMlab 125-14.
        SkimSrv uses FreqCalibration=0.9999961 for the same correction.
        """
        if 0 <= rx_index < self.n_receivers:
            cal = getattr(self, 'freq_cal', 0.9999961)  # -3.9 ppm
            self.frequencies[rx_index] = int(freq_hz * cal)

    def _send_packet(self, frame1_c0c4, frame2_c0c4):
        """Send a UDP packet with two EP2 frames."""
        pkt = build_udp_packet(self.seq, frame1_c0c4, frame2_c0c4)
        self.sock.sendto(pkt, (self.ip, self.port))
        self.seq += 1

    def start(self):
        """Start the receiver — send configuration and start command."""
        print(f"Starting HPSDR receiver at {self.ip}:{self.port}", file=sys.stderr)
        print(f"  {self.n_receivers} receivers at {SAMPLE_RATE} Hz", file=sys.stderr)

        # Send start command: 0xEF 0xFE 0x04 0x01 + zeros
        start_pkt = bytes([0xEF, 0xFE, 0x04, 0x01]) + bytes(60)
        self.sock.sendto(start_pkt, (self.ip, self.port))
        time.sleep(0.1)

        # Send initial configuration
        # C0=0x00: general config — speed, #receivers, duplex
        n_rx_bits = (self.n_receivers - 1) & 0x07
        config_c0c4 = bytes([0x00, 0x00, 0x00, 0x00, (1 << 2) | n_rx_bits])  # duplex + n_rx

        # Set LNA gain — C0 address 0x0A (sent as 0x14 = 0x0A << 1)
        # Bits 6:0 = gain in dB (0-60), bit 7 = 0
        lna_gain = getattr(self, 'lna_gain', 20)
        gain_c0c4 = bytes([0x14, 0x00, 0x00, 0x00, lna_gain & 0x7F])
        self._send_packet(config_c0c4, gain_c0c4)
        time.sleep(0.01)
        print(f"  LNA gain: {lna_gain} dB", file=sys.stderr)

        # Send frequency for each receiver
        for i in range(self.n_receivers):
            freq_c0c4 = build_freq_packet(i, self.frequencies[i])
            self._send_packet(config_c0c4, freq_c0c4)
            time.sleep(0.01)

        print(f"  Frequencies set:", file=sys.stderr)
        for i in range(self.n_receivers):
            print(f"    RX{i}: {self.frequencies[i] / 1000:.1f} kHz", file=sys.stderr)

    def stop(self):
        """Stop the receiver."""
        stop_pkt = bytes([0xEF, 0xFE, 0x04, 0x00]) + bytes(60)
        self.sock.sendto(stop_pkt, (self.ip, self.port))
        print("Receiver stopped", file=sys.stderr)

    def receive(self, callback, duration=None):
        """Receive IQ data and call callback(rx_index, iq_samples) for each receiver.

        callback receives: (rx_index, [(i, q), ...]) for each packet
        """
        start_time = time.time()
        pkt_count = 0
        last_report = start_time

        while running:
            if duration and (time.time() - start_time) >= duration:
                break

            ready = select.select([self.sock], [], [], 0.1)
            if not ready[0]:
                # Send keepalive / freq update every second
                if time.time() - last_report > 1.0:
                    config_c0c4 = bytes([0x00, 0x00, 0x00, 0x00,
                                         (1 << 2) | ((self.n_receivers - 1) & 0x07)])
                    freq_c0c4 = build_freq_packet(0, self.frequencies[0])
                    self._send_packet(config_c0c4, freq_c0c4)
                continue

            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.error:
                continue

            if len(data) < 1032:
                continue

            result = parse_iq_packet(data, self.n_receivers)
            if result is None:
                continue

            seq, all_iq = result
            pkt_count += 1

            for rx in range(self.n_receivers):
                if all_iq[rx]:
                    callback(rx, all_iq[rx])

            # Progress report every 5 seconds
            now = time.time()
            if now - last_report >= 5.0:
                elapsed = now - start_time
                rate = pkt_count / elapsed if elapsed > 0 else 0
                print(f"  {pkt_count} packets, {rate:.0f} pkt/s, "
                      f"{elapsed:.0f}s elapsed", file=sys.stderr)
                last_report = now

    def close(self):
        """Clean up."""
        self.stop()
        self.sock.close()


class IQRecorder:
    """Records IQ data from multiple receivers to WAV files or stdout."""

    def __init__(self, n_receivers, output_dir=None, wav_mode=False):
        self.n_receivers = n_receivers
        self.output_dir = output_dir
        self.wav_mode = wav_mode
        self.buffers = [[] for _ in range(n_receivers)]
        self.wav_files = [None] * n_receivers
        self.sample_counts = [0] * n_receivers

    def start_recording(self, frequencies):
        """Open WAV files for recording."""
        if not self.wav_mode:
            return

        if self.output_dir:
            os.makedirs(self.output_dir, exist_ok=True)

        for i in range(self.n_receivers):
            freq_khz = frequencies[i] / 1000
            if self.output_dir:
                path = os.path.join(self.output_dir, f'rx{i}_{freq_khz:.0f}kHz.wav')
            else:
                path = f'rx{i}_{freq_khz:.0f}kHz.wav'

            wf = wave.open(path, 'wb')
            wf.setnchannels(2)  # Stereo IQ
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(SAMPLE_RATE)
            self.wav_files[i] = wf
            print(f"  Recording RX{i} to {path}", file=sys.stderr)

    def callback(self, rx_index, iq_samples):
        """Called for each received IQ data block."""
        if self.wav_mode and self.wav_files[rx_index]:
            # Write as 16-bit stereo WAV
            for i_val, q_val in iq_samples:
                i16 = int(i_val * 32767)
                q16 = int(q_val * 32767)
                self.wav_files[rx_index].writeframes(
                    struct.pack('<hh', max(-32768, min(32767, i16)),
                                      max(-32768, min(32767, q16))))
            self.sample_counts[rx_index] += len(iq_samples)

    def stop_recording(self):
        """Close all WAV files."""
        for i in range(self.n_receivers):
            if self.wav_files[i]:
                self.wav_files[i].close()
                self.wav_files[i] = None
                print(f"  RX{i}: {self.sample_counts[i]} samples recorded",
                      file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description='HPSDR Protocol 1 IQ Receiver')
    parser.add_argument('--discover', action='store_true', help='Discover HPSDR devices')
    parser.add_argument('--ip', default='192.168.1.54', help='Device IP (default: pitaya)')
    parser.add_argument('--port', type=int, default=HPSDR_PORT, help='UDP port')
    parser.add_argument('--receivers', type=int, default=8, help='Number of receivers')
    parser.add_argument('--bands', help='Comma-separated band frequencies in kHz')
    parser.add_argument('--freq', type=int, help='Single frequency in Hz')
    parser.add_argument('--wav', action='store_true', help='Record to WAV files')
    parser.add_argument('--output-dir', help='Output directory for WAV files')
    parser.add_argument('--duration', type=int, help='Recording duration in seconds')
    parser.add_argument('--stdout', action='store_true', help='Output raw IQ to stdout')
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if args.discover:
        print("Discovering HPSDR devices...", file=sys.stderr)
        devices = discover()
        if devices:
            for d in devices:
                print(f"  {d['ip']}:{d['port']} MAC={d['mac']} "
                      f"board={d['board_id']} GW={d['gateware']} "
                      f"RX={d['receivers']}")
        else:
            print("  No devices found", file=sys.stderr)
        return

    # Set up frequencies
    if args.bands:
        bands = [int(float(f) * 1000) for f in args.bands.split(',')]
    elif args.freq:
        bands = [args.freq]
    else:
        bands = [f * 1000 for f in DEFAULT_BANDS]

    n_rx = min(args.receivers, len(bands))

    # Create receiver
    rx = HPSDRReceiver(args.ip, args.port, n_rx)
    for i, freq in enumerate(bands[:n_rx]):
        rx.set_frequency(i, freq)

    # Create recorder
    recorder = IQRecorder(n_rx, args.output_dir, args.wav)

    try:
        rx.start()
        if args.wav:
            recorder.start_recording(rx.frequencies)
        rx.receive(recorder.callback, duration=args.duration)
    finally:
        recorder.stop_recording()
        rx.close()


if __name__ == '__main__':
    main()
