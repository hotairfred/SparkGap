"""radiod_control.py: ask ka9q-radio's radiod for SparkGap's IQ channels (rtp_create_channels).

Without this, radiod's config lists each band's channel (freq = "... 14m076800 ..."), kept in step with
SparkGap's band centres by hand. With it, SparkGap sends radiod a command per band on radiod's status
group: radiod creates the channel (SSRC = centre in kHz, as rtp_receiver expects) with SparkGap's sample
rate, filter edges, encoding, no AGC and fixed gain, and drops it LIFETIME_S after the last command. The
keeper repeats the commands every REFRESH_S, so the channels last while SparkGap runs and go away by
themselves when it stops (at once on a clean stop, which ends them; else after LIFETIME_S). radiod's config then needs no channel section at all.

The wire format is ka9q-radio's status.h / status.c: one byte CMD, then TLVs (type, length, value; integers
and floats big-endian with leading zero bytes dropped, floats as their IEEE bits), then EOL. Commands go to
the status group's port 5006, where radiod listens.
"""
import ipaddress
import logging
import random
import socket
import struct
import threading

log = logging.getLogger("radiod_control")

STATUS_PORT = 5006
CMD = 1
# enum status_type (ka9q-radio status.h)
EOL, COMMAND_TAG, OUTPUT_SSRC, OUTPUT_SAMPRATE, RADIO_FREQUENCY = 0, 1, 18, 20, 33
LOW_EDGE, HIGH_EDGE, AGC_ENABLE, GAIN, PRESET, OUTPUT_ENCODING, LIFETIME = 39, 40, 62, 68, 85, 107, 117
# enum encoding (ka9q-radio rtp.h), by rtp_receiver's encoding names
ENCODING = {"s16le": 1, "s16be": 2, "f32le": 4, "f32be": 8}
BLOCK_S = 0.020          # radiod's default block time: LIFETIME counts blocks
LIFETIME_S = 60.0
REFRESH_S = 20.0


def _int(tag, x):
    """encode_int64: big-endian, leading zero bytes dropped (0 is zero length)."""
    b = int(x).to_bytes(8, "big").lstrip(b"\x00")
    return bytes([tag, len(b)]) + b


def _float(tag, x):
    return _int(tag, struct.unpack(">I", struct.pack(">f", float(x)))[0])


def _double(tag, x):
    return _int(tag, struct.unpack(">Q", struct.pack(">d", float(x)))[0])


def _string(tag, s):
    b = s.encode()
    if len(b) >= 128:
        raise ValueError("string too long")
    return bytes([tag, len(b)]) + b


def channel_command(ssrc, freq_hz, samprate=192000, low_hz=-95000.0, high_hz=95000.0, encoding="f32le",
                    gain_db=0.0, lifetime_s=LIFETIME_S, tag=None):
    """One radiod command that creates (or refreshes) an IQ channel: the "iq" preset first (radiod applies a
    PRESET before the other settings), then frequency, rate, filter edges, encoding, AGC off, gain, lifetime."""
    if encoding not in ENCODING:
        raise ValueError(f"encoding {encoding!r}: not one of {sorted(ENCODING)}")
    return b"".join([
        bytes([CMD]),
        _int(COMMAND_TAG, random.getrandbits(31) if tag is None else tag),
        _int(OUTPUT_SSRC, ssrc),
        _string(PRESET, "iq"),
        _double(RADIO_FREQUENCY, freq_hz),
        _int(OUTPUT_SAMPRATE, samprate),
        _float(LOW_EDGE, low_hz),
        _float(HIGH_EDGE, high_hz),
        _int(OUTPUT_ENCODING, ENCODING[encoding]),
        _int(AGC_ENABLE, 0),
        _float(GAIN, gain_db),
        _int(LIFETIME, round(lifetime_s / BLOCK_S)),
        bytes([EOL]),
    ])


def expire_command(ssrc, tag=None):
    """A command that ends an existing channel at radiod's next block (LIFETIME 1). Only for a channel this
    keeper made: a command naming an SSRC radiod does not have would create one."""
    return b"".join([bytes([CMD]), _int(COMMAND_TAG, random.getrandbits(31) if tag is None else tag),
                     _int(OUTPUT_SSRC, ssrc), _int(LIFETIME, 1), bytes([EOL])])


def status_addr(group, use_dns=False):
    """radiod's status group as an address: a literal, a DNS name with use_dns, else radiod's hash of the name."""
    try:
        return str(ipaddress.ip_address(group))
    except ValueError:
        if use_dns:
            return socket.gethostbyname(group)
        from rtp_receiver import radiod_maddr
        return radiod_maddr(group)


class ChannelKeeper:
    """Keeps radiod's channels for SparkGap's bands: commands at start, then every REFRESH_S on a thread.
    channels: {SSRC: centre Hz}."""

    def __init__(self, status_group, channels, samprate=192000, encoding="f32le", low_hz=-95000.0,
                 high_hz=95000.0, gain_db=0.0, iface=None, ttl=1, use_dns=False, port=STATUS_PORT,
                 refresh_s=REFRESH_S, lifetime_s=LIFETIME_S):
        self.addr = (status_addr(status_group, use_dns), int(port))
        self.group = status_group
        self.channels = dict(channels)
        self.kw = {"samprate": int(samprate), "encoding": encoding, "low_hz": float(low_hz), "high_hz": float(high_hz),
                   "gain_db": float(gain_db), "lifetime_s": float(lifetime_s)}
        self.refresh_s = float(refresh_s)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, int(ttl))
        if iface:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(_iface_addr(iface)))
        self._stop = threading.Event()
        self._thread = None
        self.sent = 0

    def set_channels(self, channels):
        """New bands (a live band change): the new channels start now, the ones no longer wanted end now."""
        gone = set(self.channels) - set(channels)
        self.channels = dict(channels)
        self.send_all()
        self._expire(gone)

    def _expire(self, ssrcs):
        for ssrc in sorted(ssrcs):
            try:
                self.sock.sendto(expire_command(ssrc), self.addr)
            except OSError as e:
                log.warning("radiod channel %d on %s: %s", ssrc, self.group, e)

    def send_all(self):
        for ssrc, hz in sorted(self.channels.items()):
            try:
                self.sock.sendto(channel_command(ssrc, hz, **self.kw), self.addr)
                self.sent += 1
            except OSError as e:
                log.warning("radiod channel %d (%.0f Hz) on %s: %s", ssrc, hz, self.group, e)

    def start(self):
        self.send_all()
        log.info("radiod: asked %s (%s:%d) for %d channels (%s), %d Hz %s, refreshed every %.0f s",
                 self.group, self.addr[0], self.addr[1], len(self.channels),
                 ", ".join(str(s) for s in sorted(self.channels)), self.kw["samprate"], self.kw["encoding"],
                 self.refresh_s)
        self._thread = threading.Thread(target=self._run, name="radiod-channels", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.refresh_s):
            self.send_all()

    def stop(self):
        """Stop refreshing and end the channels now (radiod would drop them after their lifetime anyway)."""
        if not self._stop.is_set():
            self._stop.set()
            self._expire(self.channels)


def _iface_addr(iface):
    """An interface's IPv4 address for IP_MULTICAST_IF: an address as is, lo as 127.0.0.1, else via ip(8)."""
    try:
        return str(ipaddress.ip_address(iface))
    except ValueError:
        pass
    if iface == "lo":
        return "127.0.0.1"
    import subprocess
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", iface], capture_output=True, text=True,
                         check=False).stdout.split()
    return out[out.index("inet") + 1].split("/")[0] if "inet" in out else "0.0.0.0"
