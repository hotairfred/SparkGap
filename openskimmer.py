#!/usr/bin/env python3
"""
openskimmer.py — OpenSkimmer live CW skimmer daemon.

Buffer-and-decode architecture:
    1. Accumulate 30s of IQ per band in ring buffers
    2. Every 30s: find signals via complex FFT, channelize each
    3. Run multi-speed bmorse + threshold decoder on each channel
    4. Merge all output through MASTER.SCP validation
    5. Emit validated spots on DX cluster telnet port

Usage:
    python3 openskimmer.py
    python3 openskimmer.py --config skimmer.json
    python3 openskimmer.py --ip 192.168.1.54 --bands 20m
"""

import argparse
import asyncio
import json
import logging
import math
import os
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
from scipy.signal import firwin, lfilter

from hpsdr_receiver import HPSDRReceiver, discover, SAMPLE_RATE
from telnet_server import SpotTelnetServer

log = logging.getLogger('openskimmer')

# --- Callsign validation ---

CALL_RE = re.compile(
    r'(?<![A-Z0-9])'
    r'([A-Z0-9]{1,2}\d{1,2}[A-Z]{1,3}(?:/[A-Z0-9]+)?)'
    r'(?![A-Z0-9])'
)
NOISE_RE = re.compile(r'\b[EI]\b')
CQ_PATTERNS = re.compile(r'\b(CQ|TEST|QRZ|CWT|SST|TU|UP|DE)\b', re.IGNORECASE)
FALSE_POSITIVES = {
    'CQ', 'TEST', 'QRZ', 'DE', 'TU', '5NN', '599', 'RST',
    'QSL', 'QTH', 'QRL', 'CFM', 'PSE', 'TNX', 'TKS',
    'BT', 'AR', 'SK', 'KN', 'AS', 'EE5E', 'TT5T',
}
MIN_CALL_LEN = 4

# Band centers — CW sub-band, adjusted per Grayline's note
BANDS = {
    '160m': 1820000,
    '80m':  3530000,
    '40m':  7020000,
    '30m':  10120000,
    '20m':  14030000,  # shifted down to catch 14009+
    '17m':  18080000,
    '15m':  21040000,
    '12m':  24900000,
    '10m':  28040000,
}

BMORSE_BIN = '/home/fred/morse-wip/src/bmorse'
CHANNEL_RATE = 4000  # per-channel audio sample rate for bmorse
CW_PITCH = 600       # Hz — standard CW sidetone


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


class IQBuffer:
    """Thread-safe ring buffer for IQ samples per band."""

    def __init__(self, band_name, center_freq, buffer_seconds=30):
        self.band_name = band_name
        self.center_freq = center_freq
        self.max_samples = SAMPLE_RATE * buffer_seconds
        self._lock = threading.Lock()
        self._samples = []  # list of (i, q) tuples
        self.total_received = 0

    def append(self, iq_samples):
        with self._lock:
            self._samples.extend(iq_samples)
            self.total_received += len(iq_samples)
            # Trim to max
            if len(self._samples) > self.max_samples:
                self._samples = self._samples[-self.max_samples:]

    def snapshot(self):
        """Return a copy of the buffer and clear it."""
        with self._lock:
            data = list(self._samples)
            self._samples.clear()
            return data


def find_signals(iq, sample_rate, min_snr=10):
    """Find CW signal frequencies via complex FFT.

    Returns list of (offset_hz, snr_db) sorted by SNR descending.
    """
    n = min(len(iq), 65536)
    if n < 1024:
        return []

    fft = np.fft.fft(iq[:n])
    psd = np.abs(fft) ** 2
    psd_db = 10 * np.log10(psd + 1e-20)
    noise = np.median(psd_db)

    # Find peaks above threshold
    peaks = []
    for i in range(len(fft)):
        if psd_db[i] > noise + min_snr:
            f = (i if i < len(fft) // 2 else i - len(fft)) * sample_rate / len(fft)
            peaks.append((f, psd_db[i] - noise))

    # Cluster nearby peaks (within 200 Hz)
    clustered = []
    for freq, snr in sorted(peaks, key=lambda x: x[0]):
        if not clustered or abs(freq - clustered[-1][0]) > 200:
            clustered.append((freq, snr))
        elif snr > clustered[-1][1]:
            clustered[-1] = (freq, snr)

    return sorted(clustered, key=lambda x: -x[1])


def channelize_signal(iq, sample_rate, offset_hz, target_rate=CHANNEL_RATE,
                      cw_pitch=CW_PITCH):
    """Extract one CW signal from IQ, place tone at cw_pitch Hz.

    Returns float32 audio at target_rate.
    """
    n = len(iq)
    t = np.arange(n, dtype=np.float64) / sample_rate

    # Mix signal from offset_hz to cw_pitch
    mix_freq = offset_hz - cw_pitch
    lo = np.exp(-2j * np.pi * mix_freq * t)
    mixed = np.real(iq * lo)

    # FIR lowpass + decimate
    decim = int(sample_rate) // target_rate
    if decim < 1:
        decim = 1
    nyq = sample_rate / 2.0
    cutoff = target_rate / 2.0 * 0.8
    numtaps = int(min(255, decim * 20 + 1))
    if numtaps % 2 == 0:
        numtaps += 1
    fir = firwin(numtaps, cutoff / nyq)
    filtered = lfilter(fir, 1.0, mixed)
    decimated = filtered[::decim].astype(np.float32)

    # Normalize
    peak = np.max(np.abs(decimated))
    if peak > 1e-6:
        decimated = decimated / peak * 0.9

    return decimated


def run_bmorse(audio, speed=20):
    """Run bmorse on audio array. Returns decoded text string."""
    # Write temp WAV
    tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    try:
        wf = wave.open(tmp.name, 'wb')
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(CHANNEL_RATE)
        for v in audio:
            wf.writeframes(struct.pack('<h', max(-32768, min(32767, int(v * 32767)))))
        wf.close()

        result = subprocess.run(
            [BMORSE_BIN, '-txt', '-agc', '-frq', str(CW_PITCH),
             '-spd', str(speed), tmp.name],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, Exception) as e:
        log.debug("bmorse error: %s", e)
        return ''
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


class SpotTracker:
    """Tracks callsign sightings and decides when to emit a spot."""

    def __init__(self, valid_calls, blacklist, min_sightings=1,
                 respot_interval=120):
        self.valid_calls = valid_calls
        self.blacklist = blacklist
        self.min_sightings = min_sightings
        self.respot_interval = respot_interval
        self._tracking = defaultdict(lambda: {
            'freq': 0, 'count': 0, 'last_spotted': 0, 'best_snr': 0
        })

    def process_decode(self, freq_hz, snr, text):
        """Process decoded text. Returns list of spot dicts."""
        spots = []
        clean = NOISE_RE.sub('', text.upper())

        for m in CALL_RE.finditer(clean):
            call = m.group(1)
            if len(call) < MIN_CALL_LEN or call in FALSE_POSITIVES:
                continue
            if call in self.blacklist or call not in self.valid_calls:
                continue

            info = self._tracking[call]
            info['count'] += 1
            info['freq'] = freq_hz
            info['best_snr'] = max(info['best_snr'], snr)

            now = time.time()
            has_context = bool(CQ_PATTERNS.search(clean))
            should_spot = (has_context or info['count'] >= self.min_sightings)

            if should_spot and (now - info['last_spotted']) >= self.respot_interval:
                info['last_spotted'] = now
                spots.append({
                    'call': call,
                    'freq_khz': freq_hz / 1000.0,
                    'snr': info['best_snr'],
                })
        return spots


class OpenSkimmer:
    """Main daemon — buffer-and-decode architecture."""

    def __init__(self, config):
        self.cfg = config
        self.receiver = None
        self.buffers = {}       # rx_index -> IQBuffer
        self.band_info = []     # [(name, freq_hz), ...]
        self.tracker = None
        self.telnet = None
        self.running = False
        self.spot_count = 0
        self.decode_cycles = 0
        self.start_time = None

    async def start(self):
        self.start_time = time.time()

        # Database
        calls, blacklist = load_callsign_db(
            self.cfg.get('master_scp', 'MASTER.SCP'),
            self.cfg.get('add_calls', 'add_calls.txt'),
            self.cfg.get('blacklist', 'blacklist.txt'),
        )
        self.tracker = SpotTracker(
            calls, blacklist,
            min_sightings=self.cfg.get('min_sightings', 1),
            respot_interval=self.cfg.get('respot_interval', 120),
        )

        # Telnet server
        self.telnet = SpotTelnetServer(
            port=self.cfg.get('telnet_port', 7300),
            callsign=self.cfg.get('callsign', 'WF8Z-2'),
            node_call=self.cfg.get('node_call', 'SPARK-2'),
        )
        await self.telnet.start()

        # Parse bands
        band_list = self.cfg.get('bands', ['20m'])
        for b in band_list:
            if isinstance(b, str) and b in BANDS:
                self.band_info.append((b, BANDS[b]))
            else:
                try:
                    f = int(float(b) * 1000) if isinstance(b, str) else int(b)
                    self.band_info.append((f'{f/1e6:.3f}', f))
                except ValueError:
                    log.warning("Unknown band: %s", b)

        n_rx = min(len(self.band_info), self.cfg.get('max_receivers', 8))
        self.band_info = self.band_info[:n_rx]

        # HPSDR receiver
        sdr_ip = self.cfg.get('sdr_ip', '192.168.1.54')
        devices = discover()
        if not devices:
            log.error("No HPSDR devices found")
            return False
        log.info("Found: %s MAC=%s RX=%d", devices[0]['ip'],
                 devices[0]['mac'], devices[0]['receivers'])

        self.receiver = HPSDRReceiver(sdr_ip, n_receivers=n_rx)
        buffer_secs = self.cfg.get('buffer_seconds', 30)

        for i, (name, freq) in enumerate(self.band_info):
            self.receiver.set_frequency(i, freq)
            self.buffers[i] = IQBuffer(name, freq, buffer_secs)

        # Start IQ stream
        self.receiver.start()
        self.running = True

        log.info("OpenSkimmer LIVE: %d bands, %ds buffer, telnet :%d",
                 n_rx, buffer_secs, self.cfg.get('telnet_port', 7300))
        for i, (name, freq) in enumerate(self.band_info):
            log.info("  RX%d: %s (%d Hz, ±%d kHz)",
                     i, name, freq, SAMPLE_RATE // n_rx // 2000)
        return True

    async def stop(self):
        self.running = False
        if self.receiver:
            self.receiver.close()
        if self.telnet:
            await self.telnet.stop()
        elapsed = time.time() - self.start_time if self.start_time else 0
        log.info("Stopped: %d spots, %d cycles in %.0fs",
                 self.spot_count, self.decode_cycles, elapsed)

    def _iq_callback(self, rx_index, iq_samples):
        """Called by HPSDR receiver thread — just buffer the IQ."""
        if rx_index in self.buffers:
            self.buffers[rx_index].append(iq_samples)

    def _decode_band(self, band_name, center_freq, iq_data):
        """Decode one band's buffered IQ. Returns list of (freq_hz, snr, text)."""
        if len(iq_data) < 4000:
            return []

        # Convert to complex numpy array (raw 24-bit int scale)
        iq = np.array([complex(i * 8388608, q * 8388608) for i, q in iq_data])

        # Compute actual sample rate (may differ from SAMPLE_RATE with multi-rx)
        actual_rate = len(iq_data) / self.cfg.get('buffer_seconds', 30)
        if actual_rate < 1000:
            actual_rate = SAMPLE_RATE  # fallback

        # Find signals
        min_snr = self.cfg.get('signal_min_snr', 10)
        signals = find_signals(iq, actual_rate, min_snr=min_snr)

        if not signals:
            log.debug("%s: no signals above %d dB", band_name, min_snr)
            return []

        log.info("%s: %d signals found (strongest +%.0f dB)",
                 band_name, len(signals), signals[0][1] if signals else 0)

        # Decode each signal with bmorse at multiple speeds
        bmorse_speeds = self.cfg.get('bmorse_speeds', [20, 25, 30])
        max_channels = self.cfg.get('max_channels', 15)
        results = []

        for sig_idx, (offset_hz, snr) in enumerate(signals[:max_channels]):
            actual_freq = center_freq + offset_hz
            actual_khz = actual_freq / 1000.0

            # Channelize
            audio = channelize_signal(iq, actual_rate, offset_hz)
            if len(audio) < 1000:
                continue

            # Run bmorse at each speed
            for speed in bmorse_speeds:
                decoded = run_bmorse(audio, speed=speed)
                if decoded and len(decoded) > 1:
                    results.append((actual_freq, int(snr), decoded))
                    log.debug("  %.1f kHz spd=%d: %s",
                              actual_khz, speed, decoded[:60])

        return results

    async def run(self):
        """Main loop — buffer IQ, periodically decode, emit spots."""
        # IQ receiver in background thread
        rx_thread = threading.Thread(
            target=self.receiver.receive,
            args=(self._iq_callback,),
            daemon=True,
        )
        rx_thread.start()
        log.info("IQ receiver thread started, buffering...")

        buffer_secs = self.cfg.get('buffer_seconds', 30)
        status_interval = self.cfg.get('status_interval', 30)
        last_status = time.time()

        # Wait for first buffer to fill
        await asyncio.sleep(buffer_secs + 2)

        while self.running:
            cycle_start = time.time()
            self.decode_cycles += 1
            log.info("=== Decode cycle %d ===", self.decode_cycles)

            # Snapshot all buffers
            snapshots = {}
            for rx_idx, buf in self.buffers.items():
                data = buf.snapshot()
                if data:
                    snapshots[rx_idx] = data
                    log.info("  %s: %d samples (%.1fs)",
                             buf.band_name, len(data),
                             len(data) / (SAMPLE_RATE / len(self.buffers)))

            # Decode each band (sequential — bmorse is CPU-heavy)
            for rx_idx, iq_data in snapshots.items():
                buf = self.buffers[rx_idx]
                results = self._decode_band(
                    buf.band_name, buf.center_freq, iq_data
                )

                # Process through spot tracker
                for freq_hz, snr, text in results:
                    spots = self.tracker.process_decode(freq_hz, snr, text)
                    for spot in spots:
                        self.spot_count += 1
                        self.telnet.broadcast_spot(
                            freq_khz=spot['freq_khz'],
                            dx_call=spot['call'],
                            snr=spot['snr'],
                        )
                        log.info("*** SPOT: %10.1f  %-12s  %d dB ***",
                                 spot['freq_khz'], spot['call'], spot['snr'])

            # Timing
            cycle_time = time.time() - cycle_start
            log.info("Cycle %d done in %.1fs, %d spots total",
                     self.decode_cycles, cycle_time, self.spot_count)

            # Wait for next buffer window (minus decode time)
            wait = max(1, buffer_secs - cycle_time)
            log.info("Next cycle in %.0fs", wait)
            await asyncio.sleep(wait)


def load_config(path):
    defaults = {
        'callsign': 'WF8Z-2',
        'grid': 'EM79sm',
        'node_call': 'SPARK-2',
        'sdr_ip': '192.168.1.54',
        'max_receivers': 8,
        'bands': ['20m'],
        'buffer_seconds': 30,
        'bmorse_speeds': [20, 25, 30],
        'signal_min_snr': 10,
        'max_channels': 15,
        'master_scp': 'MASTER.SCP',
        'add_calls': 'add_calls.txt',
        'blacklist': 'blacklist.txt',
        'min_sightings': 1,
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
        log.info("Signal received, shutting down...")
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
    parser.add_argument('--bands', nargs='+', help='Band list override')
    parser.add_argument('--port', type=int, help='Telnet port override')
    parser.add_argument('--callsign', help='Spotter callsign override')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )

    config = load_config(args.config if os.path.exists(args.config) else None)
    if args.ip:
        config['sdr_ip'] = args.ip
    if args.bands:
        config['bands'] = args.bands
    if args.port:
        config['telnet_port'] = args.port
    if args.callsign:
        config['callsign'] = args.callsign

    log.info("OpenSkimmer — %s @ %s, %d bands, %ds buffer, telnet :%d",
             config['callsign'], config['sdr_ip'],
             len(config['bands']), config['buffer_seconds'],
             config['telnet_port'])

    sys.exit(asyncio.run(async_main(config)))


if __name__ == '__main__':
    main()
