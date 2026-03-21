#!/usr/bin/env python3
"""
openskimmer.py — OpenSkimmer live CW skimmer daemon.

Streaming architecture with dynamic decoder instances:
    1. Continuous IQ stream from Red Pitaya via HPSDR Protocol 1
    2. Periodic FFT signal detection (every 5 seconds)
    3. One fldigi_cw process per detected signal, running continuously
    4. Wideband IQ piped to all decoder instances simultaneously
    5. Decoded text collected, validated against MASTER.SCP
    6. Spots served on DX cluster telnet port

Usage:
    python3 openskimmer.py
    python3 openskimmer.py --config skimmer.json
"""

import argparse
import asyncio
import json
import logging
import os
import re
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from hpsdr_receiver import HPSDRReceiver, discover, SAMPLE_RATE
from telnet_server import SpotTelnetServer

log = logging.getLogger('openskimmer')

CALL_RE = re.compile(
    r'(?<![A-Z0-9])([A-Z0-9]{1,2}\d{1,2}[A-Z]{1,3}(?:/[A-Z0-9]+)?)(?![A-Z0-9])'
)
FALSE_POSITIVES = {
    'CQ', 'TEST', 'QRZ', 'DE', 'TU', '5NN', '599', 'RST',
    'QSL', 'QTH', 'QRL', 'CFM', 'PSE', 'TNX', 'TKS',
    'BT', 'AR', 'SK', 'KN', 'AS', 'EE5E', 'TT5T',
}
CQ_PATTERNS = re.compile(r'\b(CQ|TEST|QRZ|CWT|SST|TU|UP|DE)\b', re.IGNORECASE)

BANDS = {
    '160m': 1820000, '80m': 3530000, '40m': 7020000, '30m': 10120000,
    '20m': 14030000, '17m': 18080000, '15m': 21040000, '12m': 24900000,
    '10m': 28040000,
}


def load_callsign_db(scp_path='MASTER.SCP', add_path='add_calls.txt',
                     blacklist_path='blacklist.txt'):
    calls = set()
    if os.path.exists(scp_path):
        with open(scp_path) as f:
            for line in f:
                line = line.strip().upper()
                if line and not line.startswith('#'):
                    calls.add(line)
    if add_path and os.path.exists(add_path):
        with open(add_path) as f:
            for line in f:
                line = line.strip().upper()
                if line:
                    calls.add(line)
    blacklist = set()
    if blacklist_path and os.path.exists(blacklist_path):
        with open(blacklist_path) as f:
            for line in f:
                line = line.strip().upper()
                if line:
                    blacklist.add(line)
    log.info("Database: %d calls + %d blacklisted", len(calls), len(blacklist))
    return calls, blacklist


class DecoderInstance:
    """One fldigi_cw process tracking one CW signal."""

    def __init__(self, freq_offset, rf_khz, sample_rate, snr,
                 decoder_bin='./fldigi_cw', bandwidth=100):
        self.freq_offset = freq_offset
        self.rf_khz = rf_khz
        self.snr = snr
        self.created = time.time()
        self.last_seen = time.time()
        self.last_output = time.time()
        self.decoded_text = ''
        self.total_chars = 0

        cmd = [
            decoder_bin,
            '-r', str(sample_rate),
            '-f', str(int(round(freq_offset))),
            '-s', '25',
            '-b', str(bandwidth),
            '-q',  # IQ mode
        ]
        self.process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )

    def feed(self, iq_pcm_bytes):
        """Feed IQ audio to the decoder."""
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write(iq_pcm_bytes)
            except (BrokenPipeError, OSError):
                pass

    def read(self):
        """Non-blocking read of decoded characters."""
        if not self.process:
            return ''
        chars = ''
        while True:
            ready, _, _ = select.select([self.process.stdout], [], [], 0)
            if not ready:
                break
            data = self.process.stdout.read(256)
            if not data:
                break
            chars += data.decode('latin-1', errors='replace')
        if chars:
            self.decoded_text += chars
            self.total_chars += len(chars)
            self.last_output = time.time()
        return chars

    def kill(self):
        if self.process:
            try:
                self.process.stdin.close()
            except:
                pass
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None


class InstanceManager:
    """Manages dynamic decoder instances per detected signal."""

    def __init__(self, sample_rate, decoder_bin='./fldigi_cw',
                 max_instances=50, signal_timeout=30, bandwidth=100):
        self.sample_rate = sample_rate
        self.decoder_bin = decoder_bin
        self.max_instances = max_instances
        self.signal_timeout = signal_timeout
        self.bandwidth = bandwidth
        self.instances = {}  # freq_key -> DecoderInstance
        self.center_khz = 0

    def update_signals(self, signals, center_khz):
        """Update instance list based on detected signals.

        signals: list of (offset_hz, snr_db) from FFT
        """
        self.center_khz = center_khz
        now = time.time()

        # Mark existing instances as seen if signal still present
        for offset, snr in signals:
            key = int(round(offset / 100)) * 100  # 100 Hz bins
            if key in self.instances:
                self.instances[key].last_seen = now
                self.instances[key].snr = snr

        # Spawn new instances for new signals
        for offset, snr in sorted(signals, key=lambda x: -x[1]):
            key = int(round(offset / 100)) * 100
            if key in self.instances:
                continue
            if len(self.instances) >= self.max_instances:
                break
            if abs(offset) < 100:  # skip DC
                continue

            rf_khz = center_khz + offset / 1000
            inst = DecoderInstance(
                offset, rf_khz, self.sample_rate, snr,
                self.decoder_bin, self.bandwidth,
            )
            self.instances[key] = inst
            log.info("Spawned decoder: %.1f kHz (offset %+.0f Hz, +%.0f dB)",
                     rf_khz, offset, snr)

        # Kill instances for signals gone > timeout
        dead = []
        for key, inst in self.instances.items():
            if now - inst.last_seen > self.signal_timeout:
                dead.append(key)
        for key in dead:
            inst = self.instances.pop(key)
            log.info("Killed decoder: %.1f kHz (gone %.0fs, %d chars decoded)",
                     inst.rf_khz, now - inst.last_seen, inst.total_chars)
            inst.kill()

    def feed_all(self, iq_pcm_bytes):
        """Feed IQ audio to ALL running decoder instances."""
        for inst in list(self.instances.values()):
            inst.feed(iq_pcm_bytes)

    def collect_all(self):
        """Read decoded text from all instances.

        Returns list of (rf_khz, snr, new_text) for instances with new output.
        """
        results = []
        for inst in list(self.instances.values()):
            text = inst.read()
            if text:
                results.append((inst.rf_khz, inst.snr, text))
        return results

    def kill_all(self):
        for inst in self.instances.values():
            inst.kill()
        self.instances.clear()

    @property
    def count(self):
        return len(self.instances)


class SpotTracker:
    """Validates and deduplicates spots."""

    def __init__(self, valid_calls, blacklist, respot_interval=120):
        self.valid_calls = valid_calls
        self.blacklist = blacklist
        self.respot_interval = respot_interval
        self._tracking = defaultdict(lambda: {
            'freq': 0, 'count': 0, 'last_spotted': 0, 'snr': 0
        })
        # Cross-channel hallucination filter
        self._cycle_calls = defaultdict(set)

    def process(self, freq_khz, snr, text):
        """Process decoded text. Returns spot dict or None."""
        clean = re.sub(r'\b[EIT]\b', '', text.upper())
        spots = []

        for m in CALL_RE.finditer(clean):
            call = m.group(1)
            if len(call) < 4 or call in FALSE_POSITIVES:
                continue
            if call in self.blacklist or call not in self.valid_calls:
                continue

            self._cycle_calls[call].add(int(freq_khz * 10))

            info = self._tracking[call]
            info['count'] += 1
            info['freq'] = freq_khz
            info['snr'] = max(info['snr'], snr)

            now = time.time()
            has_context = bool(CQ_PATTERNS.search(clean))

            if (has_context or info['count'] >= 2) and \
               (now - info['last_spotted']) >= self.respot_interval:
                # Hallucination check: same call on 3+ freqs = fake
                if len(self._cycle_calls[call]) >= 3:
                    continue
                info['last_spotted'] = now
                spots.append({
                    'call': call,
                    'freq_khz': freq_khz,
                    'snr': info['snr'],
                })

        return spots

    def reset_cycle(self):
        """Reset per-cycle hallucination tracking."""
        self._cycle_calls.clear()


class OpenSkimmer:
    """Main daemon — streaming architecture with dynamic decoder instances."""

    def __init__(self, config):
        self.cfg = config
        self.receiver = None
        self.manager = None
        self.tracker = None
        self.telnet = None
        self.running = False
        self.spot_count = 0
        self.start_time = None
        self._iq_lock = threading.Lock()
        self._iq_buffer = []

    async def start(self):
        self.start_time = time.time()

        calls, blacklist = load_callsign_db(
            self.cfg.get('master_scp', 'MASTER.SCP'),
            self.cfg.get('add_calls', 'add_calls.txt'),
            self.cfg.get('blacklist', 'blacklist.txt'),
        )
        self.tracker = SpotTracker(calls, blacklist,
                                   self.cfg.get('respot_interval', 120))

        self.telnet = SpotTelnetServer(
            port=self.cfg.get('telnet_port', 7300),
            callsign=self.cfg.get('callsign', 'WF8Z-2'),
            node_call=self.cfg.get('node_call', 'SPARK-2'),
        )
        await self.telnet.start()

        band = self.cfg.get('bands', ['20m'])[0]
        if isinstance(band, str) and band in BANDS:
            center = BANDS[band]
        else:
            center = int(band)

        devices = discover()
        if not devices:
            log.error("No HPSDR devices found")
            return False

        self.receiver = HPSDRReceiver(devices[0]['ip'], n_receivers=1)
        self.receiver.set_frequency(0, center)
        self.receiver.lna_gain = self.cfg.get('lna_gain', 20)

        self.manager = InstanceManager(
            sample_rate=SAMPLE_RATE,
            decoder_bin=self.cfg.get('decoder_bin', './fldigi_cw'),
            max_instances=self.cfg.get('max_instances', 30),
            signal_timeout=self.cfg.get('signal_timeout', 30),
            bandwidth=self.cfg.get('decoder_bandwidth', 100),
        )

        self.receiver.start()
        self.running = True

        cal_center = center * 0.9999961
        log.info("OpenSkimmer LIVE: %s (%.3f kHz), telnet :%d",
                 band, cal_center / 1000, self.cfg.get('telnet_port', 7300))
        return True

    async def stop(self):
        self.running = False
        if self.receiver:
            self.receiver.close()
        if self.manager:
            self.manager.kill_all()
        if self.telnet:
            await self.telnet.stop()
        elapsed = time.time() - self.start_time if self.start_time else 0
        log.info("Stopped: %d spots in %.0fs", self.spot_count, elapsed)

    def _iq_callback(self, rx_index, iq_samples):
        """Called from HPSDR receiver thread."""
        with self._iq_lock:
            self._iq_buffer.extend(iq_samples)
            max_buf = SAMPLE_RATE * 10
            if len(self._iq_buffer) > max_buf:
                del self._iq_buffer[:len(self._iq_buffer) - max_buf]

        # Convert to PCM and feed to all decoders
        pk = 8388608.0
        pcm = bytearray(len(iq_samples) * 4)
        for i, (iv, qv) in enumerate(iq_samples):
            i16 = max(-32768, min(32767, int(iv * pk * 4)))  # gain factor
            q16 = max(-32768, min(32767, int(qv * pk * 4)))
            struct.pack_into('<hh', pcm, i * 4, i16, q16)
        self.manager.feed_all(bytes(pcm))

    async def run(self):
        rx_thread = threading.Thread(
            target=self.receiver.receive,
            args=(self._iq_callback,),
            daemon=True,
        )
        rx_thread.start()
        log.info("IQ stream started")

        scan_interval = self.cfg.get('scan_interval', 5)
        status_interval = self.cfg.get('status_interval', 30)
        last_scan = 0
        last_status = 0

        while self.running:
            now = time.time()

            # Periodic signal scan
            if now - last_scan >= scan_interval:
                last_scan = now
                self.tracker.reset_cycle()

                with self._iq_lock:
                    if len(self._iq_buffer) >= 65536:
                        iq = np.array([complex(i * 8388608, q * 8388608)
                                       for i, q in self._iq_buffer[-65536:]])
                    else:
                        iq = None

                if iq is not None:
                    fft = np.fft.fft(iq)
                    psd_db = 10 * np.log10(np.abs(fft) ** 2 + 1e-20)
                    noise = np.median(psd_db)
                    min_snr = self.cfg.get('signal_min_snr', 12)

                    signals = []
                    N = len(fft)
                    for i in range(1, N - 1):
                        if psd_db[i] > noise + min_snr and \
                           psd_db[i] > psd_db[i - 1] and psd_db[i] > psd_db[i + 1]:
                            delta = 0.5 * (psd_db[i-1] - psd_db[i+1]) / \
                                    (psd_db[i-1] - 2*psd_db[i] + psd_db[i+1])
                            exact = i + delta
                            if exact >= N / 2:
                                exact -= N
                            f = exact * SAMPLE_RATE / N
                            signals.append((f, psd_db[i] - noise))

                    # Cluster
                    clustered = []
                    for freq, snr in sorted(signals):
                        if not clustered or abs(freq - clustered[-1][0]) > 200:
                            clustered.append((freq, snr))
                        elif snr > clustered[-1][1]:
                            clustered[-1] = (freq, snr)

                    center_khz = self.receiver.frequencies[0] * 0.9999961 / 1000
                    self.manager.update_signals(clustered, center_khz)

            # Collect decoder output
            results = self.manager.collect_all()
            for rf_khz, snr, text in results:
                spots = self.tracker.process(rf_khz, snr, text)
                for spot in spots:
                    self.spot_count += 1
                    self.telnet.broadcast_spot(
                        freq_khz=spot['freq_khz'],
                        dx_call=spot['call'],
                        snr=spot['snr'],
                    )
                    log.info("*** SPOT: %10.1f  %-12s  %d dB ***",
                             spot['freq_khz'], spot['call'], spot['snr'])

            # Status
            if now - last_status >= status_interval:
                last_status = now
                elapsed = now - self.start_time
                log.info("Status: %d spots, %d decoders, %d clients, %.0fs",
                         self.spot_count, self.manager.count,
                         self.telnet.client_count, elapsed)

            await asyncio.sleep(0.1)


def load_config(path):
    defaults = {
        'callsign': 'WF8Z-2',
        'node_call': 'SPARK-2',
        'sdr_ip': '192.168.1.54',
        'bands': ['20m'],
        'lna_gain': 20,
        'decoder_bin': './fldigi_cw',
        'decoder_bandwidth': 100,
        'max_instances': 30,
        'signal_timeout': 30,
        'signal_min_snr': 12,
        'scan_interval': 5,
        'master_scp': 'MASTER.SCP',
        'add_calls': 'add_calls.txt',
        'blacklist': 'blacklist.txt',
        'respot_interval': 120,
        'telnet_port': 7300,
        'status_interval': 30,
    }
    if path and os.path.exists(path):
        with open(path) as f:
            defaults.update(json.load(f))
    return defaults


async def async_main(config):
    skimmer = OpenSkimmer(config)

    def handle_signal():
        log.info("Shutting down...")
        skimmer.running = False

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    if not await skimmer.start():
        return 1
    try:
        await skimmer.run()
    except asyncio.CancelledError:
        pass
    finally:
        await skimmer.stop()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='OpenSkimmer — Open Source Linux CW Skimmer',
        epilog='One JSON file. One process. Zero Windows.',
    )
    parser.add_argument('--config', default='skimmer.json', help='Config JSON')
    parser.add_argument('--ip', help='SDR IP override')
    parser.add_argument('--band', help='Band override (e.g., 20m)')
    parser.add_argument('--port', type=int, help='Telnet port override')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )

    config = load_config(args.config if os.path.exists(args.config) else None)
    if args.ip:
        config['sdr_ip'] = args.ip
    if args.band:
        config['bands'] = [args.band]
    if args.port:
        config['telnet_port'] = args.port

    sys.exit(asyncio.run(async_main(config)))


if __name__ == '__main__':
    main()
