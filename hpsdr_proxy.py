#!/usr/bin/env python3
"""hpsdr_proxy.py — HPSDR Protocol 1 IQ proxy / multiplexer.

Upstream:   connects to a real pitaya as a Protocol 1 client.
Downstream: acts as a Protocol 1 server to any number of consumers
            (SkimSrv, OpenSkimmer, etc.).  Every registered consumer
            receives the same IQ stream simultaneously.

Stretch:    --wav replays a recorded WAV as fake Protocol 1 UDP frames,
            giving fully hardware-free regression tests.

Usage:
    # Live proxy (pitaya → SkimSrv + OpenSkimmer)
    python3 hpsdr_proxy.py --pitaya 192.168.1.54

    # WAV replay (regression / A-B without hardware)
    python3 hpsdr_proxy.py --wav B1_20260319_030000_7090kHz.wav
    python3 hpsdr_proxy.py --wav recording.wav --no-realtime   # as fast as possible
"""

import socket, struct, select, time, signal, sys, wave, argparse
import numpy as np

HPSDR_PORT = 1024
COOKIE     = b'\xef\xfe'
FAKE_MAC   = b'\x00\x19\x3b\x00\x00\x01'   # synthetic Hermes MAC

running = True


def _sighandler(sig, frame):
    global running
    running = False


def _discovery_reply(sock, addr, n_rx=8):
    """Respond to a Protocol 1 discovery broadcast."""
    reply = bytearray(60)
    reply[0:2] = COOKIE
    reply[2]   = 0x02       # status: not running
    reply[3:9] = FAKE_MAC
    reply[9]   = 5          # gateware version
    reply[10]  = 6          # board ID
    reply[0x13] = n_rx      # number of receivers
    sock.sendto(bytes(reply), addr)


def _iq_packet(seq, iq_list, n_rx=1):
    """Pack IQ float pairs into a 1032-byte Protocol 1 data packet (ep6 format)."""
    hdr = COOKIE + b'\x01\x06' + struct.pack('>I', seq)
    frames = bytearray()
    offset = 0
    bpg = n_rx * 6 + 2      # bytes per IQ group (6 per receiver + 2 mic)
    for _ in range(2):
        body = bytearray(504)
        pos = 0
        while pos + bpg <= 504:
            for _ in range(n_rx):
                i_f, q_f = iq_list[offset] if offset < len(iq_list) else (0.0, 0.0)
                offset += 1
                i24 = max(-8388608, min(8388607, int(i_f * 8388607)))
                q24 = max(-8388608, min(8388607, int(q_f * 8388607)))
                body[pos:pos+3] = i24.to_bytes(3, 'big', signed=True)
                body[pos+3:pos+6] = q24.to_bytes(3, 'big', signed=True)
                pos += 6
            pos += 2    # mic bytes
        frames += b'\x7f\x7f\x7f' + bytes(5) + bytes(body)
    return bytes(hdr) + bytes(frames)


# ---------------------------------------------------------------------------
# Live proxy mode
# ---------------------------------------------------------------------------

class HPSDRProxy:
    """Receives IQ from a real pitaya and fans it out to multiple consumers."""

    def __init__(self, pitaya_ip, pitaya_port=HPSDR_PORT, listen_port=HPSDR_PORT):
        self.pitaya = (pitaya_ip, pitaya_port)
        self.consumers = {}     # addr → last_seen timestamp
        self.seq = 0

        # Upstream socket: our connection to the pitaya
        self.up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.up.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.up.bind(('', 0))

        # Downstream socket: consumers connect here
        self.dn = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.dn.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.dn.bind(('', listen_port))

        print(f"Proxy :{listen_port}  →  pitaya {pitaya_ip}:{pitaya_port}", file=sys.stderr)

    def _upstream_start(self):
        self.up.sendto(b'\xef\xfe\x04\x01' + bytes(60), self.pitaya)

    def run(self):
        print("Waiting for consumers...", file=sys.stderr)
        while running:
            ready, _, _ = select.select([self.up, self.dn], [], [], 0.1)
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(2048)
                except OSError:
                    continue
                if sock is self.dn:
                    self._handle_consumer(data, addr)
                else:
                    self._forward_to_consumers(data)

    def _handle_consumer(self, data, addr):
        if len(data) < 3 or data[0:2] != COOKIE:
            return
        cmd = data[2]
        if cmd == 0x02:                             # discovery
            _discovery_reply(self.dn, addr)
            print(f"  Discovery from {addr[0]}", file=sys.stderr)
        elif cmd == 0x04 and len(data) > 3:
            if data[3] == 0x01:                     # START
                self.consumers[addr] = time.time()
                print(f"  +consumer {addr[0]}  ({len(self.consumers)} total)", file=sys.stderr)
                self._upstream_start()
            elif data[3] == 0x00:                   # STOP
                self.consumers.pop(addr, None)
                print(f"  -consumer {addr[0]}", file=sys.stderr)
        elif len(data) >= 8 and data[2:4] == b'\x01\x02':  # C&C (freq/config)
            self.up.sendto(data, self.pitaya)       # forward to pitaya

    def _forward_to_consumers(self, data):
        if len(data) < 8 or data[0:2] != COOKIE or data[2:4] != b'\x01\x06':
            return
        for caddr in list(self.consumers):
            try:
                self.dn.sendto(data, caddr)
            except OSError:
                self.consumers.pop(caddr, None)

    def close(self):
        self.up.close()
        self.dn.close()


# ---------------------------------------------------------------------------
# WAV replay mode
# ---------------------------------------------------------------------------

class WAVReplay:
    """Replay a recorded WAV file as Protocol 1 UDP to registered consumers."""

    SAMPLES_PER_PKT = 126   # 63 IQ per frame × 2 frames, n_rx=1

    def __init__(self, wav_path, listen_port=HPSDR_PORT, realtime=True,
                 start_sec=0, end_sec=None, negate_q=False):
        self.realtime = realtime
        self.negate_q = negate_q
        self.consumers = {}
        self.seq = 0

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('', listen_port))

        try:
            w = wave.open(wav_path, 'rb')
            sr, sw, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
            raw = w.readframes(w.getnframes())
            w.close()

            if sw == 3:     # 24-bit PCM — numpy has no native int24
                arr = np.frombuffer(raw, np.uint8).reshape(-1, 3)
                smp = (arr[:, 2].astype(np.int32) << 16 |
                       arr[:, 1].astype(np.int32) << 8  |
                       arr[:, 0].astype(np.int32))
                smp[smp >= 2**23] -= 2**24
                smp = smp.astype(np.float32) / 8388608.0
            else:
                dt = {2: np.int16, 4: np.int32}[sw]
                smp = np.frombuffer(raw, dt).astype(np.float32) / float(2**(sw*8-1))

            if ch == 2:
                smp = smp.reshape(-1, 2)
            else:
                smp = np.column_stack([smp, np.zeros_like(smp)])
            # Apply time window for standard WAV
            if start_sec > 0:
                smp = smp[int(start_sec * sr):]
            if end_sec:
                smp = smp[:int((end_sec - start_sec) * sr)]
        except wave.Error:
            # Extensible WAV format (0xFFFE) — use our custom reader
            from openskimmer import read_24bit_iq_chunk
            dur = end_sec - start_sec if end_sec else None
            if dur is None:
                # Read entire file — estimate duration from file size
                import os
                fsize = os.path.getsize(wav_path)
                dur = fsize / (192000 * 6)  # 24-bit stereo = 6 bytes per sample
            i_arr, q_arr = read_24bit_iq_chunk(wav_path, start_sec, dur)
            sr = 192000
            # Normalize to float (read_24bit_iq_chunk returns raw 24-bit values)
            i_f = np.array(i_arr, dtype=np.float32) / 8388608.0
            q_f = np.array(q_arr, dtype=np.float32) / 8388608.0
            smp = np.column_stack([i_f, q_f])

        self.samples = smp
        self.rate    = sr
        self.interval = self.SAMPLES_PER_PKT / sr
        print(f"WAV {wav_path}: {sr} Hz, {smp.shape[1]}ch, {len(smp)} frames",
              file=sys.stderr)

    def _handle_consumer(self, data, addr):
        if len(data) < 3 or data[0:2] != COOKIE:
            return
        cmd = data[2]
        if cmd == 0x02:
            _discovery_reply(self.sock, addr, n_rx=1)
            print(f"  Discovery from {addr[0]}", file=sys.stderr)
        elif cmd == 0x04 and len(data) > 3:
            if data[3] == 0x01:
                self.consumers[addr] = time.time()
                print(f"  +consumer {addr[0]}  ({len(self.consumers)} total)", file=sys.stderr)
            elif data[3] == 0x00:
                self.consumers.pop(addr, None)
                print(f"  -consumer {addr[0]}", file=sys.stderr)

    def run(self):
        print("WAV replay: waiting for consumers...", file=sys.stderr)
        while running and not self.consumers:
            r, _, _ = select.select([self.sock], [], [], 0.2)
            if r:
                self._handle_consumer(*self.sock.recvfrom(256))

        print("Replaying...", file=sys.stderr)
        n = len(self.samples)
        offset = 0
        nxt = time.time()

        while running and offset < n:
            r, _, _ = select.select([self.sock], [], [], 0)
            if r:
                self._handle_consumer(*self.sock.recvfrom(256))

            chunk = self.samples[offset:offset + self.SAMPLES_PER_PKT]
            # Q handling depends on recording source:
            # --negate-q: WAV recorded after hpsdr_receiver Q fix (standard IQ).
            #   Pre-negate so parse_iq_packet's negation produces standard IQ.
            # No flag: WAV recorded before Q fix (conjugate IQ from Pitaya).
            #   Don't negate — parse_iq_packet's negation converts to standard IQ.
            if self.negate_q:
                iq = [(float(row[0]), -float(row[1])) for row in chunk]
            else:
                iq = [(float(row[0]), float(row[1])) for row in chunk]
            pkt = _iq_packet(self.seq, iq)
            self.seq += 1
            offset += self.SAMPLES_PER_PKT

            for caddr in list(self.consumers):
                try:
                    self.sock.sendto(pkt, caddr)
                except OSError:
                    self.consumers.pop(caddr, None)

            if self.realtime:
                nxt += self.interval
                slp = nxt - time.time()
                if slp > 0:
                    time.sleep(slp)

        print(f"Replay complete ({offset} samples sent)", file=sys.stderr)

    def close(self):
        self.sock.close()


# ---------------------------------------------------------------------------

def main():
    signal.signal(signal.SIGINT,  _sighandler)
    signal.signal(signal.SIGTERM, _sighandler)

    ap = argparse.ArgumentParser(description='HPSDR Protocol 1 proxy / multiplexer')
    ap.add_argument('--pitaya',      default='192.168.1.54', help='Pitaya IP (live mode)')
    ap.add_argument('--port',  type=int, default=HPSDR_PORT, help='Listen port')
    ap.add_argument('--wav',         help='WAV file to replay (instead of live pitaya)')
    ap.add_argument('--no-realtime', action='store_true',    help='Replay as fast as possible')
    ap.add_argument('--start', type=float, default=0, help='Start time in seconds (WAV mode)')
    ap.add_argument('--end',   type=float, default=None, help='End time in seconds (WAV mode)')
    ap.add_argument('--negate-q', action='store_true',
                    help='Negate Q in replay (for WAVs recorded after hpsdr_receiver Q fix)')
    args = ap.parse_args()

    proxy = (WAVReplay(args.wav, args.port, realtime=not args.no_realtime,
                       start_sec=args.start, end_sec=args.end,
                       negate_q=args.negate_q)
             if args.wav else
             HPSDRProxy(args.pitaya, HPSDR_PORT, args.port))
    try:
        proxy.run()
    finally:
        proxy.close()


if __name__ == '__main__':
    main()
