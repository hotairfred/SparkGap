#!/usr/bin/env python3
"""Play IQ WAV recordings as radiod-style RTP channels, one per band (hotairfred/SparkGap#146).

Each WAV (stereo I/Q, 16- or 24-bit, as SparkGap records them) becomes one channel: SSRC = its
centre in kHz, RTP timestamp in sample frames, interleaved I/Q at radiod's packet sizes (float32
little-endian, or int16 big-endian), paced in real time.  For testing the RTP receiver without a
radiod or an RX888.

usage: rtp_iq_send.py [--group 127.0.0.1] [--port 5004] [--encoding f32le] [--speed 1]
                      [--start-min 0] [--end-min 0] [--negate-q] WAV:CENTRE_KHZ [WAV:CENTRE_KHZ ...]
"""
import argparse
import socket
import struct
import sys
import time
import wave

import numpy as np

FRAMES = {"f32le": 180, "s16be": 360}       # sample frames per packet: about 1440 bytes, under the MTU
PT = {"f32le": 111, "s16be": 112}            # dynamic payload types, as radiod assigns


def rtp_packet(pt, seq, ts, ssrc, payload, marker=False):
    return struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | pt, seq & 0xFFFF, ts & 0xFFFFFFFF,
                       ssrc) + payload


def encode(iq, encoding):
    """(n, 2) floats at 1.0 full scale -> interleaved payload bytes."""
    if encoding == "f32le":
        return iq.astype("<f4").tobytes()
    return np.clip(np.round(iq * 32768.0), -32768, 32767).astype(">i2").tobytes()


class WavIQ:
    """Chunks of a stereo IQ WAV as (n, 2) floats at 1.0 full scale."""

    def __init__(self, path, start_s=0.0):
        self.w = wave.open(path, "rb")  # noqa: SIM115 - held open for the run, read in chunks
        if self.w.getnchannels() != 2 or self.w.getsampwidth() not in (2, 3):
            raise SystemExit(f"{path}: need stereo 16- or 24-bit IQ")
        self.rate, self.width = self.w.getframerate(), self.w.getsampwidth()
        self.w.setpos(int(start_s * self.rate))

    def read(self, n):
        raw = self.w.readframes(n)
        if self.width == 2:
            return np.frombuffer(raw, dtype="<i2").astype(np.float64).reshape(-1, 2) / 32768.0
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        x = b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16)
        x = np.where(x >= 1 << 23, x - (1 << 24), x)
        return (x.astype(np.float64) / 8388608.0).reshape(-1, 2)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("channels", nargs="+", help="WAV:CENTRE_KHZ")
    ap.add_argument("--group", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5004)
    ap.add_argument("--encoding", choices=tuple(FRAMES), default="f32le")
    ap.add_argument("--speed", type=float, default=1.0, help="1 = real time")
    ap.add_argument("--start-min", type=float, default=0.0)
    ap.add_argument("--end-min", type=float, default=0.0, help="0 = the end of the shortest file")
    ap.add_argument("--negate-q", action="store_true")
    ap.add_argument("--ttl", type=int, default=1)
    a = ap.parse_args(argv)

    chans = []
    for spec in a.channels:
        path, _, khz = spec.rpartition(":")
        if not path:
            raise SystemExit(f"{spec}: expected WAV:CENTRE_KHZ")
        src = WavIQ(path, a.start_min * 60.0)
        chans.append({"src": src, "ssrc": round(float(khz)), "seq": 0, "ts": 0})
    rate = chans[0]["src"].rate
    if any(c["src"].rate != rate for c in chans):
        raise SystemExit("all recordings need the same sample rate")
    n = FRAMES[a.encoding]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, a.ttl)
    dest = (a.group, a.port)
    end_frames = int((a.end_min - a.start_min) * 60.0 * rate) if a.end_min > 0 else None
    sent, t0, chunk = 0, time.monotonic(), rate // 10              # 100 ms per read
    while end_frames is None or sent < end_frames:
        blocks = [c["src"].read(chunk) for c in chans]
        m = min(len(b) for b in blocks)
        if m == 0:
            break
        for p in range(0, m, n):
            for c, b in zip(chans, blocks):
                iq = b[p:p + n].copy()
                if a.negate_q:
                    iq[:, 1] = -iq[:, 1]
                sock.sendto(rtp_packet(PT[a.encoding], c["seq"], c["ts"], c["ssrc"],
                                       encode(iq, a.encoding), marker=c["seq"] == 0), dest)
                c["seq"] += 1
                c["ts"] += len(iq)
            due = t0 + (sent + p + n) / rate / a.speed               # pace by sample time
            lag = due - time.monotonic()
            if lag > 0.002:
                time.sleep(lag)
        sent += m
    print(f"sent {sent / rate:.1f} s of {len(chans)} channel(s) to {a.group}:{a.port}", file=sys.stderr)


if __name__ == "__main__":
    main()
