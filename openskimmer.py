#!/usr/bin/env python3
"""
openskimmer.py — OpenSkimmer live CW skimmer daemon.

Streaming architecture with dynamic decoder instances:
    1. Continuous IQ stream from Red Pitaya via HPSDR Protocol 1
    2. Periodic FFT signal detection (every 5 seconds)
    3. Per-signal SSB channelization → UHSDR decoder instances
    4. Multi-speed decoding (auto + fixed WPM) per signal
    5. Decoded text collected, validated against MASTER.SCP
    6. Spots served on DX cluster telnet port

Usage:
    python3 openskimmer.py
    python3 openskimmer.py --config skimmer.json
    python3 openskimmer.py --file B1_recording.wav --start-min 15 --end-min 30
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
from scipy.signal import decimate as scipy_decimate

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


CW_TONE = 600       # Hz — UHSDR decoder expects tone here
DECODER_RATE = 12000  # UHSDR decoder sample rate


class DecoderInstance:
    """One UHSDR decoder process tracking one CW signal.

    Performs SSB channelization (mix + decimate) on incoming IQ,
    then pipes mono audio to the uhsdr_cw decoder at DECODER_RATE.
    """

    def __init__(self, freq_offset, rf_khz, sample_rate, snr,
                 decoder_bin='./uhsdr_cw', wpm=0):
        self.freq_offset = freq_offset
        self.rf_khz = rf_khz
        self.snr = snr
        self.wpm = wpm
        self.sample_rate = sample_rate
        self.created = time.time()
        self.last_seen = time.time()
        self.last_output = time.time()
        self.decoded_text = ''
        self.total_chars = 0

        # SSB channelization state
        self.mix_freq = freq_offset - CW_TONE  # mix to put signal at CW_TONE
        self.phase = 0.0  # oscillator phase (continuous across blocks)
        self.dec_factor = sample_rate // DECODER_RATE

        # Streaming FIR lowpass for anti-aliased decimation
        # Design a lowpass at DECODER_RATE/2 = 6kHz cutoff
        from scipy.signal import firwin
        self._fir_taps = firwin(65, DECODER_RATE / 2, fs=sample_rate)
        self._fir_state = np.zeros(len(self._fir_taps) - 1)
        self._dec_buf = np.zeros(0, dtype=np.float64)
        # Running peak for normalization (slow decay, fast attack)
        self._peak = 1.0

        cmd = [decoder_bin, '-r', str(DECODER_RATE), '-f', str(CW_TONE)]
        if wpm > 0:
            cmd += ['-s', str(wpm)]
        self.process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )

    def feed_iq(self, i_samples, q_samples):
        """SSB channelize IQ and feed mono audio to decoder.

        i_samples, q_samples: numpy float64 arrays at self.sample_rate
        """
        if not self.process or self.process.poll() is not None:
            return

        n = len(i_samples)
        if n == 0:
            return

        # Generate local oscillator (continuous phase)
        t = np.arange(n) / self.sample_rate
        phase_inc = 2 * np.pi * self.mix_freq
        phases = self.phase + phase_inc * t
        self.phase = (phases[-1] + phase_inc / self.sample_rate) % (2 * np.pi)

        cos_lo = np.cos(phases)
        sin_lo = np.sin(phases)

        # Complex multiply: (I + jQ) * (cos - j*sin) = SSB demod
        mixed_real = i_samples * cos_lo + q_samples * sin_lo

        # Anti-aliased decimation: FIR lowpass then downsample
        from scipy.signal import lfilter
        filtered, self._fir_state = lfilter(
            self._fir_taps, 1.0, mixed_real, zi=self._fir_state)

        # Accumulate filtered samples for decimation
        self._dec_buf = np.concatenate([self._dec_buf, filtered])

        n_out = len(self._dec_buf) // self.dec_factor
        if n_out == 0:
            return

        usable = n_out * self.dec_factor
        decimated = self._dec_buf[:usable:self.dec_factor]  # downsample
        self._dec_buf = self._dec_buf[usable:]

        # Fixed gain normalization — let the UHSDR decoder's internal
        # Goertzel + auto-threshold handle signal levels
        # Scale so typical 24-bit signal peaks fill 16-bit range
        decimated = np.clip(decimated * 0.2, -32000, 32000)
        pcm = decimated.astype(np.int16).tobytes()

        try:
            self.process.stdin.write(pcm)
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
    """Manages dynamic UHSDR decoder instances per detected signal.

    Multi-speed: spawns multiple decoder processes per signal at different
    WPM settings. Each gets the same channelized audio.
    """

    def __init__(self, sample_rate, decoder_bin='./uhsdr_cw',
                 max_instances=150, signal_timeout=90,
                 speeds=None):
        self.sample_rate = sample_rate
        self.decoder_bin = decoder_bin
        self.max_instances = max_instances
        self.signal_timeout = signal_timeout
        self.speeds = speeds or [0, 30]  # auto + 30 WPM
        # freq_key -> list of DecoderInstance (one per speed)
        self.instances = {}
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
                for inst in self.instances[key]:
                    inst.last_seen = now
                    inst.snr = snr

        # Spawn new instance groups for new signals
        for offset, snr in sorted(signals, key=lambda x: -x[1]):
            key = int(round(offset / 100)) * 100
            if key in self.instances:
                continue
            if self.count >= self.max_instances:
                break
            if abs(offset) < 100:  # skip DC
                continue

            rf_khz = center_khz + offset / 1000
            group = []
            for wpm in self.speeds:
                inst = DecoderInstance(
                    offset, rf_khz, self.sample_rate, snr,
                    self.decoder_bin, wpm=wpm,
                )
                group.append(inst)
            self.instances[key] = group
            log.info("Spawned %d decoders: %.1f kHz (offset %+.0f Hz, +%.0f dB, speeds %s)",
                     len(group), rf_khz, offset, snr,
                     [s if s > 0 else 'auto' for s in self.speeds])

        # Kill instance groups when signal is truly gone
        dead = []
        for key, group in self.instances.items():
            last_activity = max(
                max(inst.last_seen for inst in group),
                max(inst.last_output for inst in group),
            )
            if now - last_activity > self.signal_timeout:
                dead.append(key)
        for key in dead:
            group = self.instances.pop(key)
            rf = group[0].rf_khz
            total = sum(inst.total_chars for inst in group)
            log.info("Killed %d decoders: %.1f kHz (%d chars total)",
                     len(group), rf, total)
            for inst in group:
                inst.kill()

    def feed_all_iq(self, i_samples, q_samples):
        """Feed IQ samples to ALL running decoder instances.

        Each instance does its own SSB channelization internally.
        i_samples, q_samples: numpy float64 arrays at self.sample_rate
        """
        for group in list(self.instances.values()):
            for inst in group:
                inst.feed_iq(i_samples, q_samples)

    def collect_all(self):
        """Read decoded text from all instances.

        Returns list of (rf_khz, snr, new_text, accumulated_text).
        New text for fragment accumulation, accumulated for context matching.
        """
        results = []
        for group in list(self.instances.values()):
            for inst in group:
                new_text = inst.read()
                if new_text:
                    results.append((inst.rf_khz, inst.snr, new_text,
                                    inst.decoded_text))
        return results

    def kill_all(self):
        for group in self.instances.values():
            for inst in group:
                inst.kill()
        self.instances.clear()

    @property
    def count(self):
        return sum(len(g) for g in self.instances.values())


class SpotTracker:
    """Validates spots using temporal consistency + fuzzy SCP matching.

    Two paths to a spot:
    1. Exact SCP match with context (CQ/TEST) → immediate spot
    2. Fuzzy SCP match (distance ≤ 1) + temporal consistency (3+ cycles
       at same frequency) → confident spot

    This is the streaming equivalent of the offline multi-sighting filter.
    """

    def __init__(self, valid_calls, blacklist, respot_interval=120,
                 fuzzy_min_cycles=3):
        self.valid_calls = valid_calls
        self.blacklist = blacklist
        self.respot_interval = respot_interval
        self.fuzzy_min_cycles = fuzzy_min_cycles

        # Exact match tracking
        self._tracking = defaultdict(lambda: {
            'freq': 0, 'count': 0, 'last_spotted': 0, 'snr': 0
        })

        # Temporal fragment accumulation per frequency bin (100 Hz resolution)
        # freq_bin -> {fragment: count}
        self._freq_fragments = defaultdict(lambda: defaultdict(int))
        self._freq_last_seen = defaultdict(float)

        # Cross-channel hallucination filter
        self._cycle_calls = defaultdict(set)

        # Build SCP prefix index for fast fuzzy matching
        self._scp_by_len = defaultdict(list)
        for call in valid_calls:
            self._scp_by_len[len(call)].append(call)

    def _fuzzy_match(self, fragment, max_dist=1):
        """Find SCP callsigns within edit distance max_dist of fragment.

        Optimized: first check if first 2 chars match (prefix filter),
        then compute full Levenshtein only on prefix matches.
        """
        matches = []
        prefix = fragment[:2]  # first 2 chars must match for distance ≤ 1
        for delta in [0, 1, -1]:
            target_len = len(fragment) + delta
            if target_len < 4 or target_len > 8:
                continue
            for call in self._scp_by_len.get(target_len, []):
                # Prefix filter: at least first char must match
                if call[0] != prefix[0] and (max_dist < 1 or call[0] != fragment[0]):
                    continue
                d = self._levenshtein(fragment, call)
                if d <= max_dist:
                    matches.append((call, d))
        return matches

    @staticmethod
    def _levenshtein(s1, s2):
        if len(s1) < len(s2):
            return SpotTracker._levenshtein(s2, s1)
        if len(s2) == 0:
            return len(s1)
        prev = list(range(len(s2) + 1))
        for i, c1 in enumerate(s1):
            curr = [i + 1]
            for j, c2 in enumerate(s2):
                curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(c1 != c2)))
            prev = curr
        return prev[-1]

    def process(self, freq_khz, snr, text, context_text=None):
        """Process decoded text. Returns list of spot dicts.

        text: new text fragment (1-2 chars in streaming mode)
        context_text: full accumulated text from the decoder instance
        """
        # Track processed length per frequency to avoid re-processing
        freq_bin = int(round(freq_khz * 10))
        if not hasattr(self, '_processed_len'):
            self._processed_len = {}

        full_text = context_text or text
        prev_len = self._processed_len.get(freq_bin, 0)

        # Only re-scan when we have 10+ new chars (avoid per-character overhead)
        if len(full_text) - prev_len < 10:
            return []

        self._processed_len[freq_bin] = len(full_text)

        # Only process the NEW portion for fragment accumulation
        new_text = full_text[max(0, prev_len - 10):]  # overlap 10 chars for boundary
        clean = re.sub(r'\b[EIT]\b', '', new_text.upper())
        # Full text for context matching (CQ/TEST detection)
        context_clean = re.sub(r'\b[EIT]\b', '', full_text.upper())
        spots = []
        now = time.time()

        # --- Path 1: Exact SCP match ---
        for m in CALL_RE.finditer(clean):
            call = m.group(1)
            if len(call) < 4 or call in FALSE_POSITIVES:
                continue
            if call in self.blacklist:
                continue

            if call in self.valid_calls:
                self._cycle_calls[call].add(freq_bin)
                info = self._tracking[call]
                info['count'] += 1
                info['freq'] = freq_khz
                info['snr'] = max(info['snr'], snr)

                has_context = bool(CQ_PATTERNS.search(context_clean))
                if (has_context or info['count'] >= 2) and \
                   (now - info['last_spotted']) >= self.respot_interval:
                    if len(self._cycle_calls[call]) < 3:  # hallucination check
                        info['last_spotted'] = now
                        spots.append({
                            'call': call,
                            'freq_khz': freq_khz,
                            'snr': snr,
                            'method': 'exact',
                        })

        # --- Path 2: Fragment accumulation + fuzzy match ---
        # Extract callsign-shaped fragments from collapsed text
        # (spaces in decoded text break up callsigns — collapse them)
        collapsed = re.sub(r'[^A-Z0-9]', '', clean)

        # Regex on collapsed text
        for m in CALL_RE.finditer(collapsed):
            frag = m.group(1)
            if len(frag) < 4 or frag in FALSE_POSITIVES:
                continue
            self._freq_fragments[freq_bin][frag] += 1
            self._freq_last_seen[freq_bin] = now

        # Also slide a window to catch fragments the regex misses
        for wlen in range(4, 7):
            for i in range(len(collapsed) - wlen + 1):
                frag = collapsed[i:i+wlen]
                # Must look like a callsign: has both letters and digits
                if not re.match(r'[A-Z0-9]{1,2}\d[A-Z]', frag):
                    continue
                if frag in FALSE_POSITIVES:
                    continue
                self._freq_fragments[freq_bin][frag] += 1
                self._freq_last_seen[freq_bin] = now

        # --- Path 3: Fragment clustering + consensus + SCP match ---
        # Group fragments at this frequency, find consensus via majority vote
        frags_at_freq = self._freq_fragments[freq_bin]
        if len(frags_at_freq) >= 3:
            # Group fragments by length, find most common length
            by_len = defaultdict(list)
            for frag, count in frags_at_freq.items():
                for _ in range(count):
                    by_len[len(frag)].append(frag)

            for frag_len, frag_list in by_len.items():
                if len(frag_list) < 3 or frag_len < 4:
                    continue

                # Majority vote per character position
                consensus = []
                for pos in range(frag_len):
                    chars = defaultdict(int)
                    for f in frag_list:
                        chars[f[pos]] += 1
                    best_char = max(chars, key=chars.get)
                    confidence = chars[best_char] / len(frag_list)
                    consensus.append((best_char, confidence))

                consensus_str = ''.join(c for c, _ in consensus)
                avg_confidence = sum(conf for _, conf in consensus) / len(consensus)

                if avg_confidence < 0.6:  # need at least 60% agreement
                    continue

                # Check consensus against SCP — exact first, then fuzzy
                if consensus_str in self.valid_calls:
                    info = self._tracking[consensus_str]
                    if (now - info['last_spotted']) >= self.respot_interval:
                        if consensus_str not in self.blacklist:
                            info['last_spotted'] = now
                            info['freq'] = freq_khz
                            info['snr'] = snr
                            spots.append({
                                'call': consensus_str,
                                'freq_khz': freq_khz,
                                'snr': snr,
                                'method': f'consensus(n={len(frag_list)},conf={avg_confidence:.0%})',
                            })
                            log.info("Consensus: %s (n=%d, %.0f%% conf) @ %.1f kHz",
                                     consensus_str, len(frag_list),
                                     avg_confidence * 100, freq_khz)
                            # Clear fragments for this freq after spotting
                            frags_at_freq.clear()
                else:
                    # Fuzzy SCP match on consensus
                    fuzzy = self._fuzzy_match(consensus_str, max_dist=1)
                    if fuzzy and avg_confidence >= 0.7:
                        best_call, best_dist = min(fuzzy, key=lambda x: x[1])
                        info = self._tracking[best_call]
                        if (now - info['last_spotted']) >= self.respot_interval:
                            if best_call not in self.blacklist:
                                info['last_spotted'] = now
                                info['freq'] = freq_khz
                                info['snr'] = snr
                                spots.append({
                                    'call': best_call,
                                    'freq_khz': freq_khz,
                                    'snr': snr,
                                    'method': f'fuzzy_consensus(d={best_dist},n={len(frag_list)},conf={avg_confidence:.0%})',
                                })
                                log.info("Fuzzy consensus: '%s' → %s (d=%d, n=%d, %.0f%%)",
                                         consensus_str, best_call, best_dist,
                                         len(frag_list), avg_confidence * 100)
                                frags_at_freq.clear()

        # Expire old frequency bins (>60s since last seen)
        expired = [fb for fb, t in self._freq_last_seen.items()
                   if now - t > 60]
        for fb in expired:
            del self._freq_fragments[fb]
            del self._freq_last_seen[fb]

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

        speeds_cfg = self.cfg.get('decoder_speeds', [0, 25, 30, 35])
        self.manager = InstanceManager(
            sample_rate=SAMPLE_RATE,
            decoder_bin=self.cfg.get('decoder_bin', './uhsdr_cw'),
            max_instances=self.cfg.get('max_instances', 150),
            signal_timeout=self.cfg.get('signal_timeout', 90),
            speeds=speeds_cfg,
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
        try:
            with self._iq_lock:
                self._iq_buffer.extend(iq_samples)
                max_buf = SAMPLE_RATE * 10
                if len(self._iq_buffer) > max_buf:
                    del self._iq_buffer[:len(self._iq_buffer) - max_buf]

            # Convert to numpy arrays and feed to all decoders
            n = len(iq_samples)
            i_arr = np.array([s[0] for s in iq_samples], dtype=np.float64)
            q_arr = np.array([s[1] for s in iq_samples], dtype=np.float64)
            # Scale from normalized floats to signal range
            i_arr *= 8388608.0
            q_arr *= 8388608.0
            self.manager.feed_all_iq(i_arr, q_arr)
        except Exception as e:
            pass  # Don't let decoder errors kill the receiver thread

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
            for rf_khz, snr, text, ctx in results:
                spots = self.tracker.process(rf_khz, snr, text, ctx)
                for spot in spots:
                    self.spot_count += 1
                    self.telnet.broadcast_spot(
                        freq_khz=spot['freq_khz'],
                        dx_call=spot['call'],
                        snr=spot['snr'],
                    )
                    method = spot.get('method', 'exact')
                    log.info("*** SPOT: %10.1f  %-12s  %d dB  [%s] ***",
                             spot['freq_khz'], spot['call'], spot['snr'], method)

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
        'decoder_bin': './uhsdr_cw',
        'decoder_speeds': [0, 25, 30, 35],
        'max_instances': 150,
        'signal_timeout': 90,
        'signal_min_snr': 12,
        'scan_interval': 5,
        'master_scp': 'COMBINED.SCP',
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


def read_24bit_iq_chunk(filename, start_sec, duration_sec, rate=192000):
    """Read a chunk of 24-bit stereo IQ from WAV extensible format.

    Uses numpy vectorized operations for fast 24-bit unpacking.
    """
    channels = 2
    bytes_per_sample = 3
    bytes_per_frame = bytes_per_sample * channels

    with open(filename, 'rb') as f:
        f.read(12)  # RIFF header
        while True:
            chunk_id = f.read(4)
            chunk_size = struct.unpack('<I', f.read(4))[0]
            if chunk_id == b'data':
                data_offset = f.tell()
                break
            f.seek(chunk_size, 1)

        start_frame = int(start_sec * rate)
        n_frames = int(duration_sec * rate)
        f.seek(data_offset + start_frame * bytes_per_frame)
        raw = f.read(n_frames * bytes_per_frame)

    # Fast 24-bit to int32 conversion using numpy
    raw_bytes = np.frombuffer(raw, dtype=np.uint8)
    n_samples = len(raw_bytes) // 3

    # Pad each 3-byte sample to 4 bytes (little-endian: add zero high byte)
    padded = np.zeros(n_samples * 4, dtype=np.uint8)
    padded[0::4] = raw_bytes[0::3]  # low byte
    padded[1::4] = raw_bytes[1::3]  # mid byte
    padded[2::4] = raw_bytes[2::3]  # high byte
    # padded[3::4] stays 0

    samples = padded.view(np.int32).copy()
    # Sign-extend: if bit 23 is set, subtract 2^24
    samples = samples.astype(np.float64)
    sign_mask = samples >= 0x800000
    samples[sign_mask] -= 0x1000000

    i_ch = samples[0::2]
    q_ch = samples[1::2]
    return i_ch, q_ch


def run_file_mode(args, config):
    """Offline file mode — process a WAV recording and output spots."""
    log.info("File mode: %s (%.1f-%.1f min)", args.file, args.start_min,
             args.end_min if args.end_min else 'end')

    calls, blacklist = load_callsign_db(
        config.get('master_scp', 'MASTER.SCP'),
        config.get('add_calls', 'add_calls.txt'),
        config.get('blacklist', 'blacklist.txt'),
    )
    tracker = SpotTracker(calls, blacklist, respot_interval=0)

    # Determine file format and sample rate
    with open(args.file, 'rb') as f:
        f.read(12)
        while True:
            chunk_id = f.read(4)
            chunk_size = struct.unpack('<I', f.read(4))[0]
            if chunk_id == b'fmt ':
                fmt_data = f.read(chunk_size)
                file_channels = struct.unpack('<H', fmt_data[2:4])[0]
                file_rate = struct.unpack('<I', fmt_data[4:8])[0]
                file_bits = struct.unpack('<H', fmt_data[14:16])[0]
                break
            f.seek(chunk_size, 1)

    log.info("File: %d ch, %d Hz, %d-bit", file_channels, file_rate, file_bits)

    speeds = config.get('decoder_speeds', [0, 25, 30, 35])
    manager = InstanceManager(
        sample_rate=file_rate,
        decoder_bin=config.get('decoder_bin', './uhsdr_cw'),
        max_instances=config.get('max_instances', 150),
        signal_timeout=9999,  # don't kill during file processing
        speeds=speeds,
    )

    center_khz = args.center_khz
    all_spots = []
    chunk_sec = 300  # 5-minute chunks

    start_sec = args.start_min * 60
    if args.end_min > 0:
        end_sec = args.end_min * 60
    else:
        # Calculate from file size
        with open(args.file, 'rb') as f:
            f.read(12)
            while True:
                chunk_id = f.read(4)
                chunk_size = struct.unpack('<I', f.read(4))[0]
                if chunk_id == b'data':
                    bpf = (file_bits // 8) * file_channels
                    end_sec = chunk_size / bpf / file_rate
                    break
                f.seek(chunk_size, 1)

    for t_start in np.arange(start_sec, end_sec, chunk_sec):
        t_end = min(t_start + chunk_sec, end_sec)
        dur = t_end - t_start
        log.info("Processing %.0f-%.0fs (%.1f-%.1f min)...",
                 t_start, t_end, t_start/60, t_end/60)

        if file_bits == 24:
            i_data, q_data = read_24bit_iq_chunk(args.file, t_start, dur, file_rate)
        else:
            # 16-bit standard WAV
            import wave
            w = wave.open(args.file, 'rb')
            w.setpos(int(t_start * file_rate))
            frames = w.readframes(int(dur * file_rate))
            w.close()
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float64)
            if file_channels == 2:
                i_data = samples[0::2]
                q_data = samples[1::2]
            else:
                i_data = samples
                q_data = np.zeros_like(samples)

        # FFT signal detection
        fft_size = 8192
        n_ffts = min(len(i_data) // fft_size, 200)
        avg_spectrum = np.zeros(fft_size)
        for fi in range(n_ffts):
            chunk = i_data[fi*fft_size:(fi+1)*fft_size] + \
                    1j * q_data[fi*fft_size:(fi+1)*fft_size]
            avg_spectrum += np.abs(np.fft.fft(chunk * np.hanning(fft_size))) ** 2
        avg_spectrum /= max(n_ffts, 1)
        avg_db = 10 * np.log10(avg_spectrum + 1e-20)
        freqs = np.fft.fftfreq(fft_size, 1.0 / file_rate)
        noise = np.median(avg_db)
        min_snr = config.get('signal_min_snr', 8)

        # Find peaks
        signals = []
        for i in range(1, fft_size - 1):
            if avg_db[i] > noise + min_snr and \
               avg_db[i] > avg_db[i-1] and avg_db[i] > avg_db[i+1]:
                signals.append((freqs[i], avg_db[i] - noise))

        # Cluster signals (200 Hz min spacing)
        clustered = []
        for freq, snr in sorted(signals):
            if not clustered or abs(freq - clustered[-1][0]) > 200:
                clustered.append((freq, snr))
            elif snr > clustered[-1][1]:
                clustered[-1] = (freq, snr)

        log.info("  %d signals detected", len(clustered))
        manager.update_signals(clustered, center_khz)

        # Feed IQ in blocks
        block_size = file_rate // 10  # 100ms blocks
        total_chars = 0
        total_results = 0
        for pos in range(0, len(i_data), block_size):
            i_block = i_data[pos:pos+block_size]
            q_block = q_data[pos:pos+block_size]
            manager.feed_all_iq(i_block, q_block)

            # Collect output periodically
            results = manager.collect_all()
            for rf_khz, snr, text, ctx in results:
                total_chars += len(text)
                total_results += 1
                spots = tracker.process(rf_khz, snr, text, ctx)
                for spot in spots:
                    all_spots.append(spot)
                    log.info("SPOT: %.1f kHz %s %d dB [%s]",
                             spot['freq_khz'], spot['call'],
                             spot['snr'], spot['method'])

        # Final collect after all data fed
        # Give decoders a moment to flush (send silence)
        silence = np.zeros(DECODER_RATE, dtype=np.float64)  # 1s silence
        for group in list(manager.instances.values()):
            for inst in group:
                inst._dec_buf = np.concatenate([inst._dec_buf, silence])
                # Process remaining buffer
                n_out = len(inst._dec_buf) // inst.dec_factor
                if n_out > 0:
                    usable = n_out * inst.dec_factor
                    decimated = inst._dec_buf[:usable:inst.dec_factor]
                    inst._dec_buf = inst._dec_buf[usable:]
                    if inst._peak > 0:
                        decimated = decimated / inst._peak * 16000
                    pcm = decimated.astype(np.int16).tobytes()
                    try:
                        inst.process.stdin.write(pcm)
                    except:
                        pass

        import time as _time
        _time.sleep(0.5)
        results = manager.collect_all()
        for rf_khz, snr, text, ctx in results:
            total_chars += len(text)
            total_results += 1
            spots = tracker.process(rf_khz, snr, text, ctx)
            for spot in spots:
                all_spots.append(spot)

        log.info("  Chunk decoded: %d text outputs, %d total chars, %d spots",
                 total_results, total_chars, len(all_spots))

        del i_data, q_data

    # Collect all accumulated text per signal before killing
    CALL_RE_EVAL = re.compile(r'[A-Z0-9]{1,3}\d{1,4}[A-Z]{1,4}')
    FALSE_POS_EVAL = {'CQ', 'TEST', 'QRZ', 'DE', 'TU', '5NN', '599', 'RST',
                      'QSL', 'QTH', 'QRL', 'EE5E', 'TT5T', 'NN5N'}

    decoded_calls = {}  # call -> (freq_khz, snr, text_sample)
    for key, group in manager.instances.items():
        for inst in group:
            text = inst.decoded_text.upper()
            if not text:
                continue
            # Extract callsigns from accumulated text
            # Method 1: regex extraction
            for m in CALL_RE_EVAL.finditer(text):
                call = m.group(0)
                if len(call) < 4 or call in FALSE_POS_EVAL:
                    continue
                if call in calls:  # SCP match
                    if call not in decoded_calls or inst.snr > decoded_calls[call][1]:
                        decoded_calls[call] = (inst.rf_khz, inst.snr, text[:80])
            # Method 2: sliding window — catches calls embedded in
            # noise like "TUCY0S" where regex finds "UCY0S" instead
            # Require 2+ occurrences to reduce false positives
            collapsed = re.sub(r'[^A-Z0-9]', '', text)
            for wlen in range(4, 8):
                for i in range(len(collapsed) - wlen + 1):
                    frag = collapsed[i:i+wlen]
                    if frag in calls and frag not in FALSE_POS_EVAL:
                        if frag not in decoded_calls or inst.snr > decoded_calls[frag][1]:
                            decoded_calls[frag] = (inst.rf_khz, inst.snr, text[:80])

    manager.kill_all()

    # Also include tracker spots
    for spot in all_spots:
        call = spot['call']
        if call not in decoded_calls:
            decoded_calls[call] = (spot['freq_khz'], spot['snr'], spot['method'])

    # Print results
    print(f"\n{'='*70}")
    print(f"DECODED CALLSIGNS ({len(decoded_calls)} unique, SCP-validated):")
    for call in sorted(decoded_calls, key=lambda c: decoded_calls[c][0]):
        freq, snr, _ = decoded_calls[call]
        print(f"  {freq:10.1f} kHz  {call:<12s}  {snr:3.0f} dB")

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
    parser.add_argument('--file', help='WAV file for offline testing (IQ recording)')
    parser.add_argument('--start-min', type=float, default=0, help='Start minute in file')
    parser.add_argument('--end-min', type=float, default=0, help='End minute in file (0=end)')
    parser.add_argument('--center-khz', type=float, default=7090, help='Center freq kHz for file mode')
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

    if args.file:
        sys.exit(run_file_mode(args, config))
    else:
        sys.exit(asyncio.run(async_main(config)))


if __name__ == '__main__':
    main()
