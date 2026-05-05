#!/usr/bin/env python3
"""pskr_feeder.py — PSKReporter feeder for SparkGap FT8 spots.

Subscribes to MQTT topic 'skimmer/ft8/raw', batches received spots,
encodes as IPFIX (per WSJT-X Network/PSKReporter.cpp), sends via UDP
to report.pskreporter.info:4739 every ~120s.

Reference implementation: WSJT-X PSK Reporter sender by Edson Pereira
(PY2SDR) and Bill Somerville (G4WJS). Wire format is IPFIX (RFC 7011),
big-endian, no auth, no registration. PSKReporter accepts spots from
any sender that includes a valid receiver-info record with our call+grid.

Spot payload format (JSON over MQTT):
  {
    "call":    "N4DWD",       # spotted station
    "grid":    "EM86",        # 4 or 6 char Maidenhead, "" if unknown
    "freq_hz": 7074650,       # RF frequency in Hz (dial + audio offset)
    "snr":     -5,            # signed integer dB
    "mode":    "FT8",         # mode string
    "msg":     "CQ N4DWD EM86",  # raw FT8 message (advisory)
    "ts":      1714888777     # Unix epoch seconds
  }

Run:
  python3 pskr_feeder.py --call WF8Z --grid EM79SM --dry-run    # test
  python3 pskr_feeder.py --call WF8Z --grid EM79SM              # live
"""
import argparse
import json
import logging
import os
import random
import socket
import struct
import time
from collections import deque

import paho.mqtt.client as mqtt


# === PSKReporter protocol constants (from WSJT-X PSKReporter.cpp) ===
PSKR_HOST = 'report.pskreporter.info'
PSKR_PORT = 4739
MIN_SEND_INTERVAL = 120        # seconds between batch sends
DESCRIPTOR_RESEND_INTERVAL = 3600  # hourly template re-send
DEDUP_TIMEOUT = 300            # 5 min per-call cache (matches WSJT-X)
MAX_PAYLOAD_LENGTH = 10000     # upper datagram size limit
ENTERPRISE_NUMBER = 30351      # PSK Reporter Information Element enterprise

# Set / template / link IDs (PSKReporter-specific)
SENDER_TEMPLATE_ID = 0x50e3
RECEIVER_TEMPLATE_ID = 0x50e2

# IPFIX information element IDs (need 0x8000 enterprise flag)
IE_SENDER_CALL = 1
IE_RECEIVER_CALL = 2
IE_SENDER_LOCATOR = 3
IE_RECEIVER_LOCATOR = 4
IE_FREQUENCY = 5
IE_SNR = 6
IE_DECODING_SW = 8
IE_ANTENNA_INFO = 9
IE_MODE = 10
IE_INFO_SOURCE = 11
IE_RIG_INFO = 13
# Standard IPFIX (no enterprise flag)
IE_DATE_TIME_SECONDS = 150


def pad4(buf: bytearray) -> bytearray:
    n = (4 - len(buf) % 4) % 4
    if n:
        buf.extend(b'\x00' * n)
    return buf


def write_utf_string(buf: bytearray, s: str) -> None:
    encoded = s.encode('utf-8')[:254]
    buf.append(len(encoded))
    buf.extend(encoded)


def encode_freq_5byte_be(hz: int) -> bytes:
    """5-byte big-endian frequency (40-bit unsigned, in Hz)."""
    return struct.pack('>BBBBB',
                       (hz >> 32) & 0xff, (hz >> 24) & 0xff,
                       (hz >> 16) & 0xff, (hz >> 8) & 0xff, hz & 0xff)


class IPFIXEncoder:
    """Builds IPFIX packets in the PSK Reporter dialect."""

    def __init__(self, rx_call: str, rx_grid: str, decoding_sw: str,
                 antenna: str, rig: str):
        self.rx_call = rx_call
        self.rx_grid = rx_grid
        self.decoding_sw = decoding_sw
        self.antenna = antenna
        self.rig = rig
        self.sequence_number = 0
        self.observation_id = random.randint(1, 0xFFFFFFFF)

    def _sender_template_set(self) -> bytes:
        body = bytearray()
        body.extend(struct.pack('>HH', 2, 0))           # set_id, length placeholder
        body.extend(struct.pack('>HH', 0x50e3, 7))      # link_id, field_count
        for ie, length in [
            (IE_SENDER_CALL, 0xffff),
            (IE_FREQUENCY, 5),
            (IE_SNR, 1),
            (IE_MODE, 0xffff),
            (IE_SENDER_LOCATOR, 0xffff),
            (IE_INFO_SOURCE, 1),
        ]:
            body.extend(struct.pack('>HHI', ie | 0x8000, length, ENTERPRISE_NUMBER))
        body.extend(struct.pack('>HH', IE_DATE_TIME_SECONDS, 4))
        struct.pack_into('>H', body, 2, len(body))
        return bytes(pad4(body))

    def _receiver_template_set(self) -> bytes:
        body = bytearray()
        body.extend(struct.pack('>HH', 3, 0))                 # set_id, length placeholder
        body.extend(struct.pack('>HHH', 0x50e2, 5, 0))        # link_id, field_count, scope_count
        for ie in [IE_RECEIVER_CALL, IE_RECEIVER_LOCATOR,
                   IE_DECODING_SW, IE_ANTENNA_INFO, IE_RIG_INFO]:
            body.extend(struct.pack('>HHI', ie | 0x8000, 0xffff, ENTERPRISE_NUMBER))
        struct.pack_into('>H', body, 2, len(body))
        return bytes(pad4(body))

    def _receiver_data_set(self) -> bytes:
        body = bytearray()
        body.extend(struct.pack('>HH', RECEIVER_TEMPLATE_ID, 0))
        for s in [self.rx_call, self.rx_grid, self.decoding_sw,
                  self.antenna, self.rig]:
            write_utf_string(body, s)
        struct.pack_into('>H', body, 2, len(body))
        return bytes(pad4(body))

    def _sender_data_set(self, spots: list) -> bytes:
        body = bytearray()
        body.extend(struct.pack('>HH', SENDER_TEMPLATE_ID, 0))
        for spot in spots:
            write_utf_string(body, spot['call'])
            body.extend(encode_freq_5byte_be(int(spot['freq_hz'])))
            body.extend(struct.pack('>b', max(-128, min(127, int(spot['snr'])))))
            write_utf_string(body, spot['mode'])
            write_utf_string(body, spot.get('grid', '') or '')
            body.append(1)                            # informationSource = 1 (automatic)
            body.extend(struct.pack('>I', int(spot['ts'])))
        struct.pack_into('>H', body, 2, len(body))
        return bytes(pad4(body))

    def build_packet(self, spots: list, include_templates: bool = False) -> bytes:
        self.sequence_number += 1
        msg = bytearray()
        msg.extend(struct.pack('>HH', 10, 0))                  # version=10, length placeholder
        msg.extend(struct.pack('>I', int(time.time())))        # export time
        msg.extend(struct.pack('>I', self.sequence_number))
        msg.extend(struct.pack('>I', self.observation_id))
        if include_templates:
            msg.extend(self._sender_template_set())
            msg.extend(self._receiver_template_set())
        msg.extend(self._receiver_data_set())                  # always include (server may have lost cache)
        if spots:
            msg.extend(self._sender_data_set(spots))
        pad4(msg)
        struct.pack_into('>H', msg, 2, len(msg))
        return bytes(msg)


class PSKReporterFeeder:
    def __init__(self, args):
        self.args = args
        self.encoder = IPFIXEncoder(
            rx_call=args.call,
            rx_grid=args.grid,
            decoding_sw=args.software,
            antenna=args.antenna,
            rig=args.rig,
        )
        self.spot_queue: deque = deque()
        self.dedup_cache: dict = {}                # call -> last_seen_ts
        self.last_send_ts = 0.0
        self.last_template_ts = 0.0
        self.session_start_count = 3               # send templates 3× at start (UDP loss)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.endpoint = (args.host, args.port)
        self.log = logging.getLogger('pskr_feeder')
        self.totals = {'queued': 0, 'sent_spots': 0, 'deduped': 0,
                       'packets_sent': 0, 'send_errors': 0}

    def on_mqtt_msg(self, client, userdata, msg, properties=None):
        try:
            spot = json.loads(msg.payload.decode('utf-8'))
        except Exception as e:
            self.log.warning('bad spot payload: %s', e)
            return
        required = ('call', 'freq_hz', 'snr', 'mode', 'ts')
        if not all(k in spot for k in required):
            self.log.warning('spot missing required keys: %s (have %s)',
                             required, list(spot.keys()))
            return
        # 5-min per-call dedup
        now = time.time()
        last = self.dedup_cache.get(spot['call'], 0)
        if now - last < DEDUP_TIMEOUT:
            self.totals['deduped'] += 1
            return
        self.dedup_cache[spot['call']] = now
        self.spot_queue.append(spot)
        self.totals['queued'] += 1
        self.log.debug('queued %s @ %d Hz, %s grid', spot['call'],
                       spot['freq_hz'], spot.get('grid', '?'))

    def _send_packet(self, spots: list, include_templates: bool) -> None:
        pkt = self.encoder.build_packet(spots, include_templates=include_templates)
        if len(pkt) > MAX_PAYLOAD_LENGTH:
            self.log.warning('packet %d > MAX %d, splitting next time', len(pkt), MAX_PAYLOAD_LENGTH)
        if self.args.dry_run:
            self.log.info('DRY-RUN: %d-byte packet → %s:%d, %d spots, templates=%s',
                          len(pkt), self.endpoint[0], self.endpoint[1],
                          len(spots), include_templates)
            self.log.debug('hex: %s', pkt.hex())
        else:
            self.sock.sendto(pkt, self.endpoint)
            self.log.info('sent %d-byte packet, %d spots, templates=%s',
                          len(pkt), len(spots), include_templates)
        self.totals['packets_sent'] += 1
        self.totals['sent_spots'] += len(spots)

    def maybe_send(self):
        now = time.time()
        if now - self.last_send_ts < MIN_SEND_INTERVAL:
            return
        if not self.spot_queue and self.session_start_count == 0:
            self.last_send_ts = now
            return
        # Drain queue (cap per packet to stay under MAX_PAYLOAD_LENGTH)
        spots = []
        while self.spot_queue and len(spots) < 100:
            spots.append(self.spot_queue.popleft())
        # Templates: 3× at session start, then hourly
        include_templates = (self.session_start_count > 0
                             or now - self.last_template_ts > DESCRIPTOR_RESEND_INTERVAL)
        try:
            self._send_packet(spots, include_templates=include_templates)
            if include_templates:
                self.last_template_ts = now
                if self.session_start_count > 0:
                    self.session_start_count -= 1
            self.last_send_ts = now
            # Trim dedup cache (entries older than 10 min)
            cutoff = now - 600
            self.dedup_cache = {k: v for k, v in self.dedup_cache.items() if v >= cutoff}
        except Exception as e:
            self.log.error('send failed: %s', e)
            self.totals['send_errors'] += 1
            for s in reversed(spots):
                self.spot_queue.appendleft(s)

    def run(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id='pskr_feeder')
        if self.args.mqtt_user:
            client.username_pw_set(self.args.mqtt_user, self.args.mqtt_pass or '')
        client.on_message = self.on_mqtt_msg
        client.connect(self.args.mqtt_host, self.args.mqtt_port, keepalive=30)
        client.subscribe(self.args.topic, qos=0)
        client.loop_start()
        self.log.info('PSKR feeder up: subscribed to %s on %s:%d, '
                      'sending to %s:%d (%s)',
                      self.args.topic, self.args.mqtt_host, self.args.mqtt_port,
                      self.args.host, self.args.port,
                      'DRY-RUN' if self.args.dry_run else 'LIVE')
        last_status = time.time()
        try:
            while True:
                self.maybe_send()
                # Periodic status line every ~5 min
                if time.time() - last_status > 300:
                    self.log.info('totals: %s, queue=%d', self.totals, len(self.spot_queue))
                    last_status = time.time()
                time.sleep(2)
        except KeyboardInterrupt:
            self.log.info('shutdown — final totals: %s', self.totals)
            client.loop_stop()
            client.disconnect()


def main():
    p = argparse.ArgumentParser(
        description='PSKReporter feeder for SparkGap FT8 spots')
    # Receiver identity
    p.add_argument('--call', required=True, help='Your callsign (e.g. WF8Z)')
    p.add_argument('--grid', required=True, help='Your grid square (e.g. EM79SM)')
    p.add_argument('--antenna', default='Hexbeam + 40m vertical',
                   help='Antenna description')
    p.add_argument('--rig', default='Red Pitaya 125-14 (Pavel Demin firmware)',
                   help='Rig description')
    p.add_argument('--software', default='SparkGap 0.1',
                   help='decodingSoftware field shown in PSKReporter records')
    # PSKReporter endpoint
    p.add_argument('--host', default=PSKR_HOST, help='PSKReporter host')
    p.add_argument('--port', type=int, default=PSKR_PORT, help='PSKReporter UDP port')
    # MQTT broker — defaults read from environment so credentials stay out of source.
    # Set MQTT_HOST / MQTT_PORT / MQTT_USER / MQTT_PASS in the systemd unit or shell env.
    p.add_argument('--mqtt-host', default=os.environ.get('MQTT_HOST', 'localhost'),
                   help='MQTT broker host (env: MQTT_HOST)')
    p.add_argument('--mqtt-port', type=int,
                   default=int(os.environ.get('MQTT_PORT', '1883')),
                   help='MQTT broker port (env: MQTT_PORT)')
    p.add_argument('--mqtt-user', default=os.environ.get('MQTT_USER', ''),
                   help='MQTT username (env: MQTT_USER; empty = anonymous)')
    p.add_argument('--mqtt-pass', default=os.environ.get('MQTT_PASS', ''),
                   help='MQTT password (env: MQTT_PASS)')
    p.add_argument('--topic', default='skimmer/ft8/raw',
                   help='MQTT topic to subscribe')
    # Modes
    p.add_argument('--dry-run', action='store_true',
                   help='Encode + log packets, do NOT send to PSKReporter')
    p.add_argument('--log-level', default='INFO',
                   choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = p.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format='%(asctime)s %(levelname)s %(message)s',
                        datefmt='%H:%M:%S')
    PSKReporterFeeder(args).run()


if __name__ == '__main__':
    main()
