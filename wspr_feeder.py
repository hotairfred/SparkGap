#!/usr/bin/env python3
"""wspr_feeder.py: WSPRnet uploader for SparkGap's WSPR decodes (enable_wspr).

Subscribes to MQTT topic 'skimmer/wspr/raw' (one JSON message per wsprd decode, published by sparkgap.py)
and posts each spot to WSPRnet the way WSJT-X does (Network/wsprnet.cpp): one form-encoded POST per spot
to http://wsprnet.org/post/ with function=wspr, a success reply saying "spot(s) added". WSPRnet feeds
wspr.live and the balloon trackers. Only the three message shapes WSJT-X uploads are sent:
  CALL GRID4 DBM       (type 1)
  CALL[/X] DBM         (type 2, no grid)
  <CALL> GRID6 DBM     (type 3, the call without the brackets)

Spot payload (JSON over MQTT):
  {"date": "261114", "time": "1208", "snr": -12, "dt": -0.1, "freq_hz": 14097154, "drift": 0,
   "msg": "AC1JS FN54 23", "dial_hz": 14095600, "ts": 1794744480}

Run:
  python3 wspr_feeder.py --call WF8Z --grid EM79SM --dry-run    # logs what it would send
  python3 wspr_feeder.py --call WF8Z --grid EM79SM              # live
"""
import argparse
import http.client
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.parse
import urllib.request

WSPRNET_URL = 'http://wsprnet.org/post/'
POST_GAP_S = 0.3              # between posts (WSJT-X: one every 200 ms)
RETRIES = 3                   # a failed post is tried this many times in all, RETRY_S apart
RETRY_S = 20.0
TIMEOUT_S = 20.0
DEDUP_S = 1800                # one post per cycle, call and dial
STATUS_S = 300

_TYPE1 = re.compile(r'^([A-Z0-9]{3,6})\s+([A-R]{2}\d{2})\s+(\d+)$')
_TYPE2 = re.compile(r'^([A-Z0-9/]+)\s+(\d+)$')
_TYPE3 = re.compile(r'^<([A-Z0-9/]+)>\s+([A-R]{2}\d{2}[A-X]{2})\s+(\d+)$')


def parse_message(msg):
    """(tx call, tx grid, dBm) of a WSPR message WSJT-X would upload, else None."""
    msg = ' '.join(str(msg).split()).upper()
    for rx, grid in ((_TYPE1, True), (_TYPE3, True), (_TYPE2, False)):
        m = rx.match(msg)
        if m:
            return (m[1], m[2] if grid else '', int(m[3] if grid else m[2]))
    return None


def spot_query(spot, rcall, rgrid, version):
    """The form fields WSJT-X posts for one decode, URL-encoded, or None for a message it would not post."""
    parsed = parse_message(spot.get('msg', ''))
    if parsed is None:
        return None
    tcall, tgrid, dbm = parsed
    return urllib.parse.urlencode([
        ('function', 'wspr'), ('rcall', rcall), ('rgrid', rgrid),
        ('rqrg', f"{int(spot['dial_hz']) / 1e6:.6f}"),
        ('date', str(spot['date'])), ('time', str(spot['time'])),
        ('sig', str(int(spot['snr']))), ('dt', f"{float(spot['dt']):.1f}"), ('drift', str(int(spot['drift']))),
        ('tqrg', f"{int(spot['freq_hz']) / 1e6:.6f}"),
        ('tcall', tcall), ('tgrid', tgrid), ('dbm', str(dbm)),
        ('version', version), ('mode', '2'),
    ])


class WsprFeeder:
    def __init__(self, args, post=None):
        self.args = args
        self.log = logging.getLogger('wspr_feeder')
        self.q = queue.Queue()
        self.seen = {}                       # (date, time, call, dial) -> when queued
        self.totals = {'received': 0, 'not_wsprnet_shape': 0, 'deduped': 0, 'posted': 0, 'failed': 0}
        self.post = post or self._post

    def on_spot(self, spot):
        """One decode from MQTT: dedupe, build its form, queue it."""
        self.totals['received'] += 1
        q = spot_query(spot, self.args.call, self.args.grid[:6], self.args.software)
        if q is None:
            self.totals['not_wsprnet_shape'] += 1
            return None
        key = (spot['date'], spot['time'], parse_message(spot['msg'])[0], spot['dial_hz'])
        now = time.time()
        if key in self.seen:
            self.totals['deduped'] += 1
            return None
        self.seen = {k: t for k, t in self.seen.items() if now - t < DEDUP_S}
        self.seen[key] = now
        self.q.put(q)
        return q

    def on_mqtt_msg(self, client, userdata, msg):
        try:
            self.on_spot(json.loads(msg.payload))
        except (ValueError, KeyError, TypeError) as e:
            self.log.warning('bad message: %s', e)

    def _post(self, query):
        req = urllib.request.Request(self.args.url, data=query.encode(), method='POST',
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.read().decode(errors='replace')

    def send_one(self, query):
        """Post one spot (or log it, --dry-run); True when WSPRnet took it."""
        if self.args.dry_run:
            self.log.info('DRY-RUN would post: %s', query)
            self.totals['posted'] += 1
            return True
        for attempt in range(RETRIES):
            try:
                reply = self.post(query)
                if 'spot(s) added' in reply:
                    self.totals['posted'] += 1
                    return True
                self.log.warning('WSPRnet refused (%s): %s', reply.strip()[:120], query)
                break                        # an answer that is not a network error: no retry
            except (OSError, http.client.HTTPException) as e:
                self.log.warning('post failed (try %d of %d): %s', attempt + 1, RETRIES, e)
                if attempt + 1 < RETRIES:
                    time.sleep(RETRY_S)
        self.totals['failed'] += 1
        return False

    def sender(self):
        while True:
            self.send_one(self.q.get())
            time.sleep(POST_GAP_S)

    def run(self):
        import paho.mqtt.client as mqtt
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='wspr_feeder')
        if self.args.mqtt_user:
            client.username_pw_set(self.args.mqtt_user, self.args.mqtt_pass or '')
        client.on_message = self.on_mqtt_msg
        client.connect(self.args.mqtt_host, self.args.mqtt_port, keepalive=30)
        client.subscribe(self.args.topic, qos=0)
        client.loop_start()
        threading.Thread(target=self.sender, daemon=True).start()
        self.log.info('WSPR feeder up: %s on %s:%d -> %s (%s), %s %s', self.args.topic, self.args.mqtt_host,
                      self.args.mqtt_port, self.args.url, 'DRY-RUN' if self.args.dry_run else 'LIVE',
                      self.args.call, self.args.grid)
        try:
            while True:
                time.sleep(STATUS_S)
                self.log.info('totals: %s, queue=%d', self.totals, self.q.qsize())
        except KeyboardInterrupt:
            self.log.info('shutdown, totals: %s', self.totals)
            client.loop_stop()
            client.disconnect()


def main():
    p = argparse.ArgumentParser(description='WSPRnet uploader for SparkGap WSPR decodes (MQTT skimmer/wspr/raw)')
    p.add_argument('--call', required=True, help='Your callsign (the reporter)')
    p.add_argument('--grid', required=True, help='Your grid square, 4 or 6 characters')
    p.add_argument('--software', default='SparkGap', help='Version string WSPRnet records')
    p.add_argument('--url', default=WSPRNET_URL, help='WSPRnet post URL')
    p.add_argument('--mqtt-host', default=os.environ.get('MQTT_HOST', 'localhost'), help='MQTT broker (env: MQTT_HOST)')
    p.add_argument('--mqtt-port', type=int, default=int(os.environ.get('MQTT_PORT', '1883')),
                   help='MQTT port (env: MQTT_PORT)')
    p.add_argument('--mqtt-user', default=os.environ.get('MQTT_USER', ''), help='MQTT user (env: MQTT_USER)')
    p.add_argument('--mqtt-pass', default=os.environ.get('MQTT_PASS', ''), help='MQTT password (env: MQTT_PASS)')
    p.add_argument('--topic', default='skimmer/wspr/raw', help='MQTT topic')
    p.add_argument('--dry-run', action='store_true', help='Log what would be posted; post nothing')
    p.add_argument('--log-level', default='INFO')
    args = p.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format='%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S')
    WsprFeeder(args).run()


if __name__ == '__main__':
    main()
