#!/usr/bin/env python3
"""wsjtx_udp.py: SparkGap's FT8, FT4 and WSPR decodes as WSJT-X UDP messages.

Programs written for WSJT-X (GridTracker, JTAlert, N1MM Logger+, the RBN Aggregator's FT# page, JS8Call
tools, ...) listen for WSJT-X's UDP messages, by default on port 2237. This sends SparkGap's decodes to them
in that format, so they can read SparkGap as they read WSJT-X.

It reads the decodes SparkGap publishes on MQTT (skimmer/ft8/raw for FT8 and FT4, skimmer/wspr/raw for WSPR,
the topics pskr_feeder.py and wspr_feeder.py read) and sends, per band and mode, what one WSJT-X instance
would: a Heartbeat and a Status every 15 s (the dial frequency and mode), a Decode (or WSPRDecode) per
decode, and a Close when it stops. Each band and mode is its own instance, named by --id with the band and
mode after it ("SparkGap 20m FT8"), because a WSJT-X instance has one dial.

The wire format is WSJT-X's Network/NetworkMessage.hpp: a QDataStream (Qt 5.4, schema 2), big-endian.

Run:
  python3 wsjtx_udp.py                                   # to 127.0.0.1:2237
  python3 wsjtx_udp.py --to 192.168.1.205:2237 --to 224.0.0.73:2237
  python3 wsjtx_udp.py --call WF8Z --grid EM79 --dry-run # print, send nothing
"""
import argparse
import json
import logging
import os
import socket
import struct
import threading
import time

MAGIC = 0xADBCCBDA
SCHEMA = 2
HEARTBEAT, STATUS, DECODE, CLEAR, REPLY, QSO_LOGGED, CLOSE = 0, 1, 2, 3, 4, 5, 6
WSPR_DECODE = 10
MAX_SCHEMA = 3
DECODE_MODE = {'FT8': '~', 'FT4': '+'}      # the mode character WSJT-X puts in a Decode
TR_PERIOD_S = {'FT8': 15, 'FT4': 7, 'WSPR': 120}
log = logging.getLogger('wsjtx_udp')
STATUS_EVERY_S = 15.0
IDLE_CLOSE_S = 600.0                        # an instance silent this long is closed (its band went quiet)

# band by dial kHz, for instance names
BANDS = [(1800, 2000, '160m'), (3500, 4000, '80m'), (5250, 5450, '60m'), (7000, 7300, '40m'),
         (10100, 10150, '30m'), (14000, 14350, '20m'), (18068, 18168, '17m'), (21000, 21450, '15m'),
         (24890, 24990, '12m'), (28000, 29700, '10m'), (50000, 54000, '6m')]


def band_of(khz):
    for lo, hi, name in BANDS:
        if lo <= khz <= hi:
            return name
    return f'{int(khz)} kHz'


# QDataStream pieces
def utf8(s):
    if s is None:
        return struct.pack('>I', 0xFFFFFFFF)
    b = s.encode('utf-8')
    return struct.pack('>I', len(b)) + b


def qbool(v):
    return b'\x01' if v else b'\x00'


def qtime_ms(epoch):
    """QTime: ms since UTC midnight."""
    return round((epoch % 86400) * 1000) % 86400000


def header(kind, ident):
    return struct.pack('>III', MAGIC, SCHEMA, kind) + utf8(ident)


def heartbeat(ident, version, revision=''):
    return header(HEARTBEAT, ident) + struct.pack('>I', MAX_SCHEMA) + utf8(version) + utf8(revision)


def status(ident, dial_hz, mode, de_call='', de_grid='', df_hz=1500):
    return (header(STATUS, ident) + struct.pack('>Q', int(dial_hz)) + utf8(mode)
            + utf8('') + utf8('') + utf8(mode)               # DX call, report, Tx mode
            + qbool(False) + qbool(False) + qbool(True)      # Tx enabled, transmitting, decoding
            + struct.pack('>II', df_hz, df_hz)               # Rx DF, Tx DF
            + utf8(de_call) + utf8(de_grid) + utf8('')       # DE call, DE grid, DX grid
            + qbool(False) + utf8('') + qbool(False)         # Tx watchdog, sub-mode, fast mode
            + struct.pack('>B', 0)                           # special operation mode: none
            + struct.pack('>I', 0xFFFFFFFF)                  # frequency tolerance: not set
            + struct.pack('>I', TR_PERIOD_S.get(mode, 15))   # T/R period
            + utf8('SparkGap') + utf8(''))                   # configuration name, Tx message


def decode(ident, epoch, snr, dt, df_hz, mode, msg, new=True):
    return (header(DECODE, ident) + qbool(new) + struct.pack('>I', qtime_ms(epoch))
            + struct.pack('>i', int(snr)) + struct.pack('>d', float(dt)) + struct.pack('>I', int(df_hz))
            + utf8(DECODE_MODE.get(mode, mode)) + utf8(msg) + qbool(False) + qbool(False))


def wspr_decode(ident, epoch, snr, dt, freq_hz, drift, call, grid, dbm, new=True):
    return (header(WSPR_DECODE, ident) + qbool(new) + struct.pack('>I', qtime_ms(epoch))
            + struct.pack('>i', int(snr)) + struct.pack('>d', float(dt)) + struct.pack('>Q', int(freq_hz))
            + struct.pack('>i', int(drift)) + utf8(call) + utf8(grid) + struct.pack('>i', int(dbm))
            + qbool(False))


def close(ident):
    return header(CLOSE, ident)


def ftx_spot(d):
    """(dial Hz, DF Hz, slot epoch, DT, mode, snr, message) of a skimmer/ft8/raw payload. Older SparkGap
    builds publish no dial: the dial is then the standard one below the frequency, from the band's list."""
    mode = d.get('mode', 'FT8')
    f = int(d['freq_hz'])
    dial = d.get('dial_hz')
    df = d.get('df_hz')
    if dial is None:
        dial = std_dial_hz(f, mode)
        df = f - dial
    slot = d.get('slot_ts')
    if slot is None:                          # no slot time: the slot before the publish
        p = 7.5 if mode == 'FT4' else 15.0
        slot = (float(d.get('ts', time.time())) // p - 1) * p
    return int(dial), int(df), float(slot), float(d.get('dt', 0.0)), mode, int(d.get('snr', 0)), d.get('msg', '')


FT8_DIALS_KHZ = [1840, 3573, 5357, 7074, 10136, 14074, 18100, 21074, 24915, 28074, 50313]
FT4_DIALS_KHZ = [3575, 7047.5, 10140, 14080, 18104, 21140, 24919, 28180, 50318]


def std_dial_hz(freq_hz, mode):
    dials = FT4_DIALS_KHZ if mode == 'FT4' else FT8_DIALS_KHZ
    below = [k for k in dials if 0 <= freq_hz - k * 1000 <= 5000]
    return int(max(below) * 1000) if below else int(freq_hz // 1000 * 1000)


def wspr_fields(msg):
    """(call, grid, dBm) of a WSPR message: 'K1ABC FN42 37', or the type 2/3 forms 'PJ4/K1ABC 37' and
    '<K1ABC> FN42AB 37' (the grid empty when the message has none)."""
    parts = msg.split()
    dbm = int(parts[-1]) if parts and parts[-1].lstrip('-').isdigit() else 0
    call = parts[0] if parts else ''
    grid = parts[1] if len(parts) >= 3 else ''
    return call, grid, dbm


class Sender:
    def __init__(self, dests, ident, call, grid, version, dry_run=False, ttl=1):
        self.dests, self.ident, self.call, self.grid = dests, ident, call, grid
        self.version, self.dry_run = version, dry_run
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        self.inst = {}                        # name -> {'dial', 'mode', 'last'}
        self.lock = threading.Lock()
        self.sent = 0

    def send(self, pkt, what=''):
        if self.dry_run:
            log.info("would send %d bytes: %s", len(pkt), what)
            return
        for d in self.dests:
            try:
                self.sock.sendto(pkt, d)
                self.sent += 1
            except OSError as e:
                log.warning("send to %s:%d: %s", d[0], d[1], e)

    def instance(self, dial_hz, mode):
        """The instance for this dial and mode, announced (Heartbeat + Status) when it is new."""
        name = f'{self.ident} {band_of(dial_hz / 1000)} {mode}'
        with self.lock:
            it = self.inst.get(name)
            fresh = it is None or it['dial'] != dial_hz
            self.inst[name] = {'dial': dial_hz, 'mode': mode, 'last': time.time()}
        if fresh:
            self.announce(name, dial_hz, mode)
        return name

    def announce(self, name, dial_hz, mode):
        self.send(heartbeat(name, self.version), 'heartbeat ' + name)
        self.send(status(name, dial_hz, mode, self.call, self.grid), f'status {name} {dial_hz} Hz')

    def tick(self):
        """Every STATUS_EVERY_S: Heartbeat + Status per live instance; Close for those gone quiet."""
        now = time.time()
        with self.lock:
            items = list(self.inst.items())
        for name, it in items:
            if now - it['last'] > IDLE_CLOSE_S:
                self.send(close(name), 'close ' + name)
                with self.lock:
                    self.inst.pop(name, None)
            else:
                self.announce(name, it['dial'], it['mode'])

    def close_all(self):
        with self.lock:
            names = list(self.inst)
        for name in names:
            self.send(close(name), 'close ' + name)

    def on_ftx(self, d):
        dial, df, slot, dt, mode, snr, msg = ftx_spot(d)
        if mode not in DECODE_MODE or not msg:
            return
        name = self.instance(dial, mode)
        self.send(decode(name, slot, snr, dt, df, mode, msg), f'{name} {snr:+d} {dt:.1f} {df} {msg}')

    def on_wspr(self, d):
        dial = int(d.get('dial_hz') or d['freq_hz'] // 1000 * 1000)
        call, grid, dbm = wspr_fields(d.get('msg', ''))
        if not call:
            return
        name = self.instance(dial, 'WSPR')
        self.send(wspr_decode(name, float(d.get('ts', time.time())), d.get('snr', 0), d.get('dt', 0.0),
                              d['freq_hz'], d.get('drift', 0), call, grid, dbm),
                  f'{name} {call} {grid} {dbm}')


def parse_dest(s):
    host, _, port = s.rpartition(':')
    return (host or '127.0.0.1', int(port)) if port.isdigit() else (s, 2237)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--to', action='append', default=[], metavar='HOST:PORT',
                   help='where to send (repeat for more; a multicast address works); default 127.0.0.1:2237')
    p.add_argument('--id', default='SparkGap', help='instance name, the band and mode follow it')
    p.add_argument('--call', default='', help='your call, shown as DE call in the Status')
    p.add_argument('--grid', default='', help='your grid, shown as DE grid in the Status')
    p.add_argument('--version', default='SparkGap', help='version string in the Heartbeat')
    p.add_argument('--ttl', type=int, default=1, help='multicast TTL')
    p.add_argument('--mqtt-host', default=os.environ.get('MQTT_HOST', 'localhost'))
    p.add_argument('--mqtt-port', type=int, default=int(os.environ.get('MQTT_PORT', '1883')))
    p.add_argument('--mqtt-user', default=os.environ.get('MQTT_USER', ''))
    p.add_argument('--mqtt-pass', default=os.environ.get('MQTT_PASS', ''))
    p.add_argument('--no-wspr', action='store_true', help='leave out skimmer/wspr/raw')
    p.add_argument('--dry-run', action='store_true', help='log what would be sent, send nothing')
    p.add_argument('--log-level', default='INFO')
    a = p.parse_args()
    logging.basicConfig(level=getattr(logging, a.log_level.upper(), logging.INFO),
                        format='%(asctime)s %(levelname)s %(message)s')
    import paho.mqtt.client as mqtt

    dests = [parse_dest(s) for s in a.to] or [('127.0.0.1', 2237)]
    snd = Sender(dests, a.id, a.call.upper(), a.grid, a.version, a.dry_run, a.ttl)
    topics = {'skimmer/ft8/raw': snd.on_ftx}
    if not a.no_wspr:
        topics['skimmer/wspr/raw'] = snd.on_wspr

    def on_message(client, userdata, msg):
        try:
            topics[msg.topic](json.loads(msg.payload))
        except (KeyError, ValueError, TypeError) as e:
            log.debug("bad payload on %s: %s", msg.topic, e)

    def on_connect(client, userdata, flags, rc, properties=None):
        for t in topics:
            client.subscribe(t, qos=0)
        log.info("MQTT %s:%d: %s -> %s", a.mqtt_host, a.mqtt_port, ', '.join(topics),
                     ', '.join(f'{h}:{pt}' for h, pt in dests))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'wsjtx-udp-{os.getpid()}')
    if a.mqtt_user:
        client.username_pw_set(a.mqtt_user, a.mqtt_pass or '')
    client.on_message = on_message
    client.on_connect = on_connect
    client.connect(a.mqtt_host, a.mqtt_port, keepalive=30)
    client.loop_start()
    try:
        while True:
            time.sleep(STATUS_EVERY_S)
            snd.tick()
    except KeyboardInterrupt:
        pass
    finally:
        snd.close_all()
        client.loop_stop()


if __name__ == '__main__':
    main()
