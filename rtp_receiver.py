"""IQ from ka9q-radio's radiod over RTP: one channel per band (hotairfred/SparkGap#146).

radiod (https://github.com/ka9q/ka9q-radio) drives a wideband receiver such as the RX888, does the
channel filtering and sends each channel as RTP, usually to a multicast group on port 5004.  A
channel's SSRC is its frequency in kHz unless the radiod config sets another, and its RTP timestamp
counts sample frames.  An `iq` channel carries interleaved I and Q, float32 little-endian or int16
big-endian; radiod gives the payload type a dynamic number, so the encoding comes from the config.

RtpReceiver is a drop-in for hpsdr_receiver.HPSDRReceiver and flex_iq.FlexIQReceiver: start(), then
receive(callback) calls callback(rx_index, iq) per packet with iq an (n, 2) float64 array at 1.0
full scale.  A packet lost in transit (an RTP sequence gap) is filled with zeros up to MAX_FILL_S,
so a band's time line stays whole; a late or repeated packet is dropped.  A stream that starts over
(radiod restarted: its sequence and timestamp jump) is followed at once.  The first packets of each
channel are checked against rtp_encoding (bytes per sample frame) and, after RATE_CHECK_S, against
sample_rate (timestamp frames per second); a channel that stops for STALL_S is logged.
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
FRAME_BYTES = {"f32le": 8, "s16be": 4}
RESYNC_LATE = 50      # this many late packets in a row: the stream started over, follow it
RATE_CHECK_S = 10.0   # wall-clock seconds of a channel before its timestamp rate is checked
STALL_S = 10.0        # a channel silent this long mid-run is logged


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


def radiod_maddr(name: str) -> str:
    """The multicast address radiod gives a group name (ka9q-radio multicast.c make_maddr): FNV-1
    of the name in 239.0.0.0/8, off the two /24s that share Ethernet addresses with 224.0.0.0/24.
    radiod hashes names unless its config says dns = yes, so SparkGap does too (rtp_use_dns)."""
    h = 0x811C9DC5
    for b in name.encode():
        h = (h * 0x01000193) & 0xFFFFFFFF
        h ^= b
    addr = (239 << 24) | (h & 0xFFFFFF)
    if addr & 0x007FFF00 == 0:
        addr |= (addr & 0xFF) << 8
    if addr & 0x007FFF00 == 0:
        addr |= 0x00100000
    return socket.inet_ntoa(struct.pack("!I", addr))


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
                 ssrcs=None, use_dns=False):
        if encoding not in ENCODINGS:
            raise ValueError(f"rtp_encoding must be one of {ENCODINGS}, got {encoding!r}")
        self.group = group
        self.port = int(port)
        self.sample_rate = int(sample_rate)
        self.encoding = encoding
        self.iface = iface
        self.use_dns = use_dns         # a group name: look it up, else hash it as radiod does
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
        self.resyncs = 0       # streams that started over (radiod restarted)
        self._seq = {}         # SSRC -> last sequence number
        self._ts = {}          # SSRC -> timestamp after the last packet's samples
        self._late_run = {}    # SSRC -> late packets in a row
        self._first = {}       # SSRC -> (wall time, timestamp, frames since) for the rate check
        self._seen = {}        # SSRC -> wall time of its last packet
        self._checked = set()  # SSRCs whose rate is checked
        self._stalled = set()  # SSRCs logged as silent

    # -- tuning: radiod's config sets the channels; this only says which SSRC is which band --
    def set_frequency(self, rx_index, freq_hz):
        self.frequencies[rx_index] = freq_hz
        self._by_ssrc = {}
        for k, f in sorted(self.frequencies.items()):
            ssrc = self._fixed_ssrc.get(k, ssrc_for(f))
            if ssrc in self._by_ssrc:
                log.warning("RTP: bands %d and %d both map to SSRC %d (%.0f and %.0f Hz): band %d gets "
                            "none; give radiod's channels distinct kHz or set rtp_ssrcs", self._by_ssrc[ssrc], k,
                            ssrc, self.frequencies[self._by_ssrc[ssrc]], f, self._by_ssrc[ssrc])
            self._by_ssrc[ssrc] = k

    def retune(self, freq_hz):
        """One band: follow a channel at another frequency (radiod must carry it)."""
        self.set_frequency(0, freq_hz)
        self._seq.clear()
        self._ts.clear()

    # -- lifecycle --
    def _open(self):
        try:
            addr = str(ipaddress.ip_address(self.group))
        except ValueError:
            addr = socket.gethostbyname(self.group) if self.use_dns else radiod_maddr(self.group)
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
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, self._membership(addr))
        else:
            s.bind((addr, self.port))          # unicast: a sender pointed at this address
        return s

    def _membership(self, addr):
        """IP_ADD_MEMBERSHIP for the group on rtp_iface: an address, a host name, or an interface name
        (eth0, lo), joined by index (Linux ip_mreqn)."""
        group = socket.inet_aton(addr)
        if not self.iface:
            return group + socket.inet_aton("0.0.0.0")
        try:
            return group + socket.inet_aton(str(ipaddress.ip_address(self.iface)))
        except ValueError:
            pass
        try:
            idx = socket.if_nametoindex(self.iface)
        except OSError:
            return group + socket.inet_aton(socket.gethostbyname(self.iface))
        return struct.pack("=4s4si", group, socket.inet_aton("0.0.0.0"), idx)

    def start(self):
        if self.sock is None:
            self.sock = self._open()
            log.info("RTP: listening on %s:%d (%s, %s, %d Hz), SSRCs %s", self.group, self.port,
                     self.sock.getsockname()[0], self.encoding, self.sample_rate, sorted(self._by_ssrc))
        self._running = True

    def stop(self):
        self._running = False

    def close(self):
        self._running = False
        if self.packets or self.other:
            log.info("RTP: %d packets, %d lost (filled), %d late (dropped), %d for other SSRCs, %d restarts",
                     self.packets, self.lost, self.late, self.other, self.resyncs)
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def health(self):
        return {"packets": self.packets, "lost": self.lost, "late": self.late, "other": self.other,
                "resyncs": self.resyncs, "last_packet": self.last_packet}

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
        now = time.time()
        last = self._seq.get(ssrc)
        if last is not None:
            d = (seq - last) & 0xFFFF
            jump = (ts - self._ts[ssrc]) & 0xFFFFFFFF
            jump = min(jump, 0x100000000 - jump)         # timestamp distance either way
            if d == 0 or d >= 0x8000:              # a repeat, or older than the last one
                run = self._late_run.get(ssrc, 0) + 1
                if jump <= MAX_FILL_S * self.sample_rate and run < RESYNC_LATE:
                    self._late_run[ssrc] = run
                    self.late += 1
                    return
                self._restart(ssrc, rx, "sequence and timestamp jumped back" if run < RESYNC_LATE
                              else f"{run} late packets in a row")
            elif d > 1:
                self.lost += d - 1
                missing = (ts - self._ts[ssrc]) & 0xFFFFFFFF
                if 0 < missing <= MAX_FILL_S * self.sample_rate:
                    callback(rx, np.zeros((missing, 2)))
        self._late_run[ssrc] = 0
        self._check(ssrc, rx, ts, len(payload), last is not None and d == 1, now)
        self._seq[ssrc] = seq
        self._ts[ssrc] = (ts + len(iq)) & 0xFFFFFFFF
        self.packets += 1
        self.last_packet = now
        self._seen[ssrc] = now
        if ssrc in self._stalled:
            self._stalled.discard(ssrc)
            log.info("RTP: band %d (SSRC %d) packets again", rx, ssrc)
        if len(iq):
            callback(rx, iq)

    def _restart(self, ssrc, rx, why):
        """The stream started over: forget its sequence, timestamp and rate check."""
        self.resyncs += 1
        log.warning("RTP: band %d (SSRC %d) started over (%s): following the new stream", rx, ssrc, why)
        self._first.pop(ssrc, None)
        self._checked.discard(ssrc)

    def _check(self, ssrc, rx, ts, nbytes, consecutive, now):
        """Once per stream: bytes per sample frame against rtp_encoding (the timestamp step between two
        consecutive packets), then the timestamp rate against sample_rate over RATE_CHECK_S."""
        if ssrc in self._checked:
            return
        st = self._first.get(ssrc)
        if st is None or now - st[4] > 1.0:                  # new, or after a pause: the rate restarts
            self._first[ssrc] = [now, ts, ts, st[3] if st else False, now]   # wall start, first ts, last ts,
            return                                                           # encoding checked, last wall
        t0, ts0, prev, enc_ok, _ = st
        st[2], st[4] = ts, now
        step = (ts - prev) & 0xFFFFFFFF
        if not enc_ok and consecutive and 0 < step < 1 << 16:
            st[3] = True
            if step * FRAME_BYTES[self.encoding] != nbytes:
                per = nbytes / step
                guess = {8: "f32le", 4: "s16be"}.get(round(per))
                log.error("RTP: band %d (SSRC %d) packets carry %.1f bytes per sample frame, not %d for "
                          "rtp_encoding %s%s: its IQ is garbage until the config matches radiod's", rx, ssrc,
                          per, FRAME_BYTES[self.encoding], self.encoding,
                          f" (looks like {guess})" if guess else "")
                self._checked.add(ssrc)
                return
        if now - t0 >= RATE_CHECK_S:
            self._checked.add(ssrc)
            rate = ((ts - ts0) & 0xFFFFFFFF) / (now - t0)
            if abs(rate / self.sample_rate - 1.0) > 0.15:     # a wrong samprate is off by 2x or more
                log.error("RTP: band %d (SSRC %d) runs at about %.0f samples/s, not sample_rate %d: set "
                          "radiod's samprate or SparkGap's sample_rate so they match (frequencies are wrong "
                          "until then)", rx, ssrc, rate, self.sample_rate)

    def _stalls(self, now):
        for ssrc, t in self._seen.items():
            if ssrc not in self._stalled and now - t > STALL_S:
                self._stalled.add(ssrc)
                log.warning("RTP: band %d (SSRC %d) no packets for %.0f s", self._by_ssrc.get(ssrc, -1), ssrc,
                            now - t)

    def receive(self, callback, duration=None):
        """Receive until stop() or duration seconds; callback(rx_index, iq) per packet."""
        if self.sock is None:
            self.start()
        start, warned = time.time(), False
        self._last_stall_check = start
        while self._running:
            if duration and time.time() - start >= duration:
                break
            if not self.packets and not warned and time.time() - start > 10:
                warned = True
                log.warning("RTP: no packets for SSRCs %s on %s:%d after 10 s (%d for others): check "
                            "radiod's channel frequencies, the group and the interface",
                            sorted(self._by_ssrc), self.group, self.port, self.other)
            now = time.time()
            if now - self._last_stall_check >= 1.0:
                self._last_stall_check = now
                self._stalls(now)
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
