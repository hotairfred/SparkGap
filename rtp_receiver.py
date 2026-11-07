"""IQ from ka9q-radio's radiod over RTP: one channel per band (hotairfred/SparkGap#146).

radiod (https://github.com/ka9q/ka9q-radio) drives a wideband receiver such as the RX888, does the
channel filtering and sends each channel as RTP, usually to a multicast group on port 5004.  A
channel's SSRC is its frequency in kHz unless the radiod config sets another, and its RTP timestamp
counts sample frames.  An `iq` channel carries interleaved I and Q, float32 little-endian or int16
big-endian; radiod gives the payload type a dynamic number, so the encoding comes from the config.

RtpReceiver is a drop-in for hpsdr_receiver.HPSDRReceiver and flex_iq.FlexIQReceiver: start(), then
receive(callback) calls callback(rx_index, iq) per packet with iq an (n, 2) float64 array at 1.0
full scale.  A packet lost in transit (an RTP sequence gap) is filled with zeros up to MAX_FILL_S,
so a band's time line stays whole; a late or repeated packet is dropped.
"""

import ipaddress
import logging
import select
import socket
import struct
import time

import numpy as np

log = logging.getLogger("sparkgap")

ENCODINGS = ("f32le", "s16be")
MAX_FILL_S = 0.5      # a sequence gap longer than this is a restart, not filled
RCVBUF = 8 << 20


def parse_rtp(pkt: bytes):
    """(payload type, sequence, timestamp, ssrc, payload) of an RTP packet, or None if it is not one."""
    if len(pkt) < 12:
        return None
    b0, b1, seq, ts, ssrc = struct.unpack_from("!BBHII", pkt)
    if b0 >> 6 != 2:
        return None
    off = 12 + 4 * (b0 & 0x0F)                         # CSRC list
    if b0 & 0x10:                                     # header extension
        if len(pkt) < off + 4:
            return None
        off += 4 + 4 * struct.unpack_from("!H", pkt, off + 2)[0]
    end = len(pkt)
    if b0 & 0x20:                                     # padding
        end -= pkt[-1]
    if off > end:
        return None
    return b1 & 0x7F, seq, ts, ssrc, pkt[off:end]


def decode(payload: bytes, encoding: str) -> np.ndarray:
    """Interleaved I/Q payload -> (n, 2) float64 at 1.0 full scale."""
    if encoding == "f32le":
        a = np.frombuffer(payload, dtype="<f4", count=len(payload) // 8 * 2)
        return a.astype(np.float64).reshape(-1, 2)
    if encoding == "s16be":
        a = np.frombuffer(payload, dtype=">i2", count=len(payload) // 4 * 2)
        return (a.astype(np.float64) / 32768.0).reshape(-1, 2)
    raise ValueError(f"rtp_encoding must be one of {ENCODINGS}, got {encoding!r}")


def ssrc_for(freq_hz: float) -> int:
    """radiod's default SSRC for a channel: its frequency in kHz."""
    return round(freq_hz / 1000.0)


class RtpReceiver:
    """radiod IQ channels over RTP, one per band, picked by SSRC.

    Usage:
        rx = RtpReceiver('239.1.2.3', sample_rate=192000)
        rx.set_frequency(0, 14_090_000)
        rx.start()
        rx.receive(callback)   # blocks
        rx.stop()
    """

    def __init__(self, group, port=5004, sample_rate=192000, encoding="f32le", iface=None,
                 ssrcs=None):
        if encoding not in ENCODINGS:
            raise ValueError(f"rtp_encoding must be one of {ENCODINGS}, got {encoding!r}")
        self.group = group
        self.port = int(port)
        self.sample_rate = int(sample_rate)
        self.encoding = encoding
        self.iface = iface
        self._fixed_ssrc = dict(ssrcs or {})   # rx_index -> SSRC, when radiod's config sets its own
        self.frequencies = {}                  # rx_index -> Hz
        self._by_ssrc = {}                     # SSRC -> rx_index
        self.sock = None
        self._running = False
        # health
        self.packets = 0
        self.lost = 0          # packets missing (sequence gaps), filled with zeros when short
        self.late = 0          # packets out of order or repeated, dropped
        self.other = 0         # packets for an SSRC no band has
        self.last_packet = None
        self._seq = {}         # SSRC -> last sequence number
        self._ts = {}          # SSRC -> timestamp after the last packet's samples

    # -- tuning: radiod's config sets the channels; this only says which SSRC is which band --
    def set_frequency(self, rx_index, freq_hz):
        self.frequencies[rx_index] = freq_hz
        self._by_ssrc = {self._fixed_ssrc.get(k, ssrc_for(f)): k for k, f in self.frequencies.items()}

    def retune(self, freq_hz):
        """One band: follow a channel at another frequency (radiod must carry it)."""
        self.set_frequency(0, freq_hz)
        self._seq.clear()
        self._ts.clear()

    # -- lifecycle --
    def _open(self):
        addr = socket.gethostbyname(self.group)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
        except OSError:
            pass
        got = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        if got < RCVBUF // 2:      # Linux reports double what it grants; capped by net.core.rmem_max
            log.warning("RTP: receive buffer %d kB, not %d kB: packets may be lost on busy bands; raise it "
                        "with 'sudo sysctl -w net.core.rmem_max=16777216' (and /etc/sysctl.d/ to keep it)",
                        got // 1024, RCVBUF // 1024)
        if ipaddress.ip_address(addr).is_multicast:
            s.bind((addr, self.port))
            local = socket.inet_aton(socket.gethostbyname(self.iface) if self.iface else "0.0.0.0")
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(addr) + local)
        else:
            s.bind((addr, self.port))          # unicast: a sender pointed at this address
        return s

    def start(self):
        if self.sock is None:
            self.sock = self._open()
            log.info("RTP: listening on %s:%d (%s, %d Hz), SSRCs %s", self.group, self.port,
                     self.encoding, self.sample_rate, sorted(self._by_ssrc))
        self._running = True

    def stop(self):
        self._running = False

    def close(self):
        self._running = False
        if self.packets or self.other:
            log.info("RTP: %d packets, %d lost (filled), %d late (dropped), %d for other SSRCs",
                     self.packets, self.lost, self.late, self.other)
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def health(self):
        return {"packets": self.packets, "lost": self.lost, "late": self.late, "other": self.other,
                "last_packet": self.last_packet}

    # -- receive --
    def handle(self, pkt, callback):
        """One datagram: decode it, fill a short gap before it, hand it to callback."""
        parsed = parse_rtp(pkt)
        if parsed is None:
            return
        _pt, seq, ts, ssrc, payload = parsed
        rx = self._by_ssrc.get(ssrc)
        if rx is None:
            self.other += 1
            return
        iq = decode(payload, self.encoding)
        last = self._seq.get(ssrc)
        if last is not None:
            d = (seq - last) & 0xFFFF
            if d == 0 or d >= 0x8000:              # a repeat, or older than the last one
                self.late += 1
                return
            if d > 1:
                self.lost += d - 1
                missing = (ts - self._ts[ssrc]) & 0xFFFFFFFF
                if 0 < missing <= MAX_FILL_S * self.sample_rate:
                    callback(rx, np.zeros((missing, 2)))
        self._seq[ssrc] = seq
        self._ts[ssrc] = (ts + len(iq)) & 0xFFFFFFFF
        self.packets += 1
        self.last_packet = time.time()
        if len(iq):
            callback(rx, iq)

    def receive(self, callback, duration=None):
        """Receive until stop() or duration seconds; callback(rx_index, iq) per packet."""
        if self.sock is None:
            self.start()
        start, warned = time.time(), False
        while self._running:
            if duration and time.time() - start >= duration:
                break
            if not self.packets and not warned and time.time() - start > 10:
                warned = True
                log.warning("RTP: no packets for SSRCs %s on %s:%d after 10 s (%d for others): check "
                            "radiod's channel frequencies, the group and the interface",
                            sorted(self._by_ssrc), self.group, self.port, self.other)
            sock = self.sock
            try:
                if sock is None or not select.select([sock], [], [], 0.2)[0]:
                    continue
                pkt = sock.recv(65536)
            except (OSError, ValueError):          # closed under us by close(): shutting down
                if not self._running or self.sock is None:
                    break
                raise
            self.handle(pkt, callback)
