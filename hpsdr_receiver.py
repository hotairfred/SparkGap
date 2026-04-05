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

# Supported sample rates (C1 bits 1:0 speed field):
# 00=48kHz, 01=96kHz, 10=192kHz, 11=384kHz
SAMPLE_RATE = 48000
_SPEED_BITS = {48000: 0, 96000: 1, 192000: 2, 384000: 3}

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

    HPSDR Protocol 1 C0 address map (C0 = address<<1 | MOX):
      C0=0x00: general config (address 0)
      C0=0x02: TX NCO frequency (address 1)  -- NOT RX
      C0=0x04: RX1 NCO frequency (address 2)
      C0=0x06: RX2 NCO frequency (address 3)
    C1-C4 = frequency in Hz (big-endian 32-bit)
    """
    # RX1=0x04, RX2=0x06, ... (address = rx_index+2, C0 = address<<1)
    c0 = bytes([(rx_index + 2) * 2])
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

    # Each frame: 8-byte header (sync+C&C) + 504 bytes IQ data
    # IQ data: repeating groups of (n_receivers * 6 bytes IQ) + 2 bytes mic
    bytes_per_group = n_receivers * 6 + 2
    all_i = [[] for _ in range(n_receivers)]
    all_q = [[] for _ in range(n_receivers)]

    for frame_offset in [8, 520]:  # Two 512-byte frames per packet
        frame_bytes = np.frombuffer(data, dtype=np.uint8,
                                    offset=frame_offset + 8, count=504)
        n_groups = len(frame_bytes) // bytes_per_group
        if n_groups == 0:
            continue

        # Reshape to (n_groups, bytes_per_group), drop mic bytes (last 2 per group)
        groups = frame_bytes[:n_groups * bytes_per_group].reshape(n_groups, bytes_per_group)
        iq_data = groups[:, :n_receivers * 6].reshape(n_groups * n_receivers, 6)

        # Reconstruct 24-bit signed I from big-endian bytes [0,1,2]
        i_vals = ((iq_data[:, 0].astype(np.int32) << 16) |
                  (iq_data[:, 1].astype(np.int32) << 8) |
                   iq_data[:, 2].astype(np.int32))
        i_vals = np.where(i_vals >= 0x800000, i_vals - 0x1000000, i_vals)

        # Reconstruct 24-bit signed Q from big-endian bytes [3,4,5]
        q_vals = ((iq_data[:, 3].astype(np.int32) << 16) |
                  (iq_data[:, 4].astype(np.int32) << 8) |
                   iq_data[:, 5].astype(np.int32))
        q_vals = np.where(q_vals >= 0x800000, q_vals - 0x1000000, q_vals)

        i_f = i_vals.astype(np.float32) / 8388608.0
        q_f = -q_vals.astype(np.float32) / 8388608.0

        # Deinterleave: rows are [grp0_rx0, grp0_rx1, ..., grp0_rxN, grp1_rx0, ...]
        i_by_rx = i_f.reshape(n_groups, n_receivers)
        q_by_rx = q_f.reshape(n_groups, n_receivers)

        for rx in range(n_receivers):
            all_i[rx].extend(i_by_rx[:, rx].tolist())
            all_q[rx].extend(q_by_rx[:, rx].tolist())

    all_iq = [list(zip(all_i[rx], all_q[rx])) for rx in range(n_receivers)]
    return seq, all_iq


class HPSDRReceiver:
    """HPSDR Protocol 1 receiver for Red Pitaya.

    In passive mode, the receiver listens to an existing IQ stream (e.g. from
    hpsdr_proxy.py) without sending START/STOP or configuration commands.
    Another application (e.g. SkimSrv) drives the hardware — we just ride along.

    The rx_filter parameter selects which receiver channel(s) to deliver.
    When set, only the specified receiver index is passed to the callback.
    """

    def __init__(self, ip, port=HPSDR_PORT, n_receivers=8, sample_rate=48000,
                 listen_port=None, passive=False, rx_filter=None):
        self.ip = ip
        self.port = port              # remote port (send to)
        self.listen_port = listen_port or port  # local bind port (receive on)
        self.n_receivers = n_receivers
        self.sample_rate = sample_rate if sample_rate in _SPEED_BITS else 48000
        self.passive = passive
        self.rx_filter = rx_filter    # None = all receivers, int = single receiver
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        self.sock.bind(('', self.listen_port))
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
        """Start the receiver — send configuration and start command.

        In passive mode, skips all C&C commands — just listens for IQ packets
        from an upstream source (proxy or direct).
        """
        if self.passive:
            mode = f"PASSIVE (rx_filter={self.rx_filter})" if self.rx_filter is not None else "PASSIVE (all receivers)"
            print(f"Starting HPSDR receiver at {self.ip}:{self.port} — {mode}", file=sys.stderr)
            print(f"  {self.n_receivers} receivers at {self.sample_rate} Hz", file=sys.stderr)
            print(f"  No C&C — listening only", file=sys.stderr)
            return

        print(f"Starting HPSDR receiver at {self.ip}:{self.port}", file=sys.stderr)
        print(f"  {self.n_receivers} receivers at {self.sample_rate} Hz", file=sys.stderr)

        # Send start command: 0xEF 0xFE 0x04 0x01 + zeros
        start_pkt = bytes([0xEF, 0xFE, 0x04, 0x01]) + bytes(60)
        self.sock.sendto(start_pkt, (self.ip, self.port))
        print(f"  START pkt: {start_pkt[:8].hex(' ')}", file=sys.stderr)
        time.sleep(0.1)

        # Send initial configuration
        # C0=0x00: general config — speed, #receivers, duplex
        # C1 bits 1:0 = speed: 0=48k, 1=96k, 2=192k, 3=384k
        n_rx_bits = (self.n_receivers - 1) & 0x07
        speed_bits = _SPEED_BITS.get(self.sample_rate, 0)
        config_c0c4 = bytes([0x00, speed_bits, 0x00, 0x00, (1 << 2) | n_rx_bits])  # duplex + n_rx

        # Set LNA gain — C0 address 0x0A (sent as 0x14 = 0x0A << 1)
        # Bits 6:0 = gain in dB (0-60), bit 7 = 0
        lna_gain = getattr(self, 'lna_gain', 20)
        gain_c0c4 = bytes([0x14, 0x00, 0x00, 0x00, lna_gain & 0x7F])
        print(f"  config C0-C4: {config_c0c4.hex(' ')}  lna C0-C4: {gain_c0c4.hex(' ')}", file=sys.stderr)
        self._send_packet(config_c0c4, gain_c0c4)
        time.sleep(0.01)
        print(f"  LNA gain: {lna_gain} dB", file=sys.stderr)

        # Send frequency for each receiver
        for i in range(self.n_receivers):
            freq_c0c4 = build_freq_packet(i, self.frequencies[i])
            print(f"  freq C0-C4 RX{i}: {freq_c0c4.hex(' ')}  ({self.frequencies[i]/1000:.3f} kHz)", file=sys.stderr)
            self._send_packet(config_c0c4, freq_c0c4)
            time.sleep(0.01)

        print(f"  Frequencies set:", file=sys.stderr)
        for i in range(self.n_receivers):
            print(f"    RX{i}: {self.frequencies[i] / 1000:.1f} kHz", file=sys.stderr)

    def stop(self):
        """Stop the receiver. In passive mode, just closes — no STOP command."""
        if not self.passive:
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
                # Send keepalive / freq update every second (active mode only)
                if not self.passive and time.time() - last_report > 1.0:
                    speed_bits = _SPEED_BITS.get(self.sample_rate, 0)
                    config_c0c4 = bytes([0x00, speed_bits, 0x00, 0x00,
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
                    # In filtered mode, only deliver the selected receiver
                    if self.rx_filter is not None and rx != self.rx_filter:
                        continue
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
