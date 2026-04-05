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

import faulthandler
faulthandler.enable()

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
import itertools
from collections import defaultdict, deque
from datetime import datetime, timezone

import numpy as np
from scipy.signal import decimate as scipy_decimate

from hpsdr_receiver import HPSDRReceiver, discover
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
    '160m': 1891000, '80m': 3591000, '40m': 7100000, '30m': 10191000,
    '20m': 14091000, '17m': 18159000, '15m': 21091000, '12m': 24981000,
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


CW_TONE = 700       # Hz — matches natural tone placement from channelizer
DECODER_RATE = 12000  # UHSDR decoder sample rate
BMORSE_RATE = 4000    # bmorse input sample rate


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
        # Tap count scales with dec_factor so stopband attenuation stays good
        # at higher sample rates (65 taps @ 48kHz, 257 taps @ 192kHz)
        from scipy.signal import firwin
        n_taps = self.dec_factor * 4 + 1  # always odd
        self._fir_taps = firwin(n_taps, DECODER_RATE / 2, fs=sample_rate)
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
        # WAV capture: dump first 10s of audio for diagnostic
        self._wav_writer = None
        if wpm == 0 and not os.path.exists('/tmp/decoder_audio.wav'):
            import wave as _wave
            self._wav_writer = _wave.open('/tmp/decoder_audio.wav', 'wb')
            self._wav_writer.setnchannels(1)
            self._wav_writer.setsampwidth(2)
            self._wav_writer.setframerate(DECODER_RATE)
            self._wav_samples = 0

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
        decimated = np.clip(decimated * 10.0, -32000, 32000)
        pcm = decimated.astype(np.int16).tobytes()

        try:
            self.process.stdin.write(pcm)
        except (BrokenPipeError, OSError):
            pass

        # WAV capture for diagnostic — first 10 seconds
        if getattr(self, '_wav_writer', None):
            self._wav_samples += len(decimated)
            self._wav_writer.writeframes(pcm)
            if self._wav_samples >= DECODER_RATE * 10:
                self._wav_writer.close()
                self._wav_writer = None
                log.info("WAV capture complete: /tmp/decoder_audio.wav (%.1f kHz)", self.rf_khz)

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


class BmorseInstance:
    """One bmorse decoder process tracking one CW signal via stdin.

    Channelizes IQ to 4kHz mono (peak-normalized) and pipes to bmorse.
    Same feed_iq/read/kill duck-type interface as DecoderInstance.
    """

    def __init__(self, freq_offset, rf_khz, sample_rate, snr,
                 bmorse_bin='/home/fred/morse-wip/src/bmorse', wpm=30):
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

        # Channelization state — mix to CW_TONE, decimate to BMORSE_RATE
        self.mix_freq = freq_offset - CW_TONE
        self.phase = 0.0
        self.dec_factor = sample_rate // BMORSE_RATE

        from scipy.signal import firwin
        n_taps = self.dec_factor * 4 + 1
        self._fir_taps = firwin(n_taps, BMORSE_RATE / 2, fs=sample_rate)
        self._fir_state = np.zeros(len(self._fir_taps) - 1)
        self._dec_buf = np.zeros(0, dtype=np.float64)

        # Peak tracker for normalization (fast attack, slow decay)
        self._peak = 1.0

        cmd = [bmorse_bin, '-stdin', '-txt',
               '-spd', str(wpm if wpm > 0 else 30),
               '-frq', str(CW_TONE),
               '-rate', str(BMORSE_RATE)]
        self.process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )

    def feed_iq(self, i_samples, q_samples):
        """SSB channelize IQ to 4kHz and feed to bmorse stdin."""
        if not self.process or self.process.poll() is not None:
            return
        n = len(i_samples)
        if n == 0:
            return

        t = np.arange(n) / self.sample_rate
        phase_inc = 2 * np.pi * self.mix_freq
        phases = self.phase + phase_inc * t
        self.phase = (phases[-1] + phase_inc / self.sample_rate) % (2 * np.pi)

        mixed_real = i_samples * np.cos(phases) + q_samples * np.sin(phases)

        from scipy.signal import lfilter
        filtered, self._fir_state = lfilter(
            self._fir_taps, 1.0, mixed_real, zi=self._fir_state)

        self._dec_buf = np.concatenate([self._dec_buf, filtered])
        n_out = len(self._dec_buf) // self.dec_factor
        if n_out == 0:
            return

        usable = n_out * self.dec_factor
        decimated = self._dec_buf[:usable:self.dec_factor]
        self._dec_buf = self._dec_buf[usable:]

        # Peak normalization to 0.8 (bmorse needs relative amplitude)
        peak = np.max(np.abs(decimated)) if len(decimated) > 0 else 0.0
        if peak > self._peak:
            self._peak = peak
        else:
            self._peak = 0.9999 * self._peak + 0.0001 * peak  # slow decay
        if self._peak > 0:
            decimated = decimated / self._peak * 0.8

        pcm = np.clip(decimated * 32767, -32767, 32767).astype(np.int16).tobytes()
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


class HamFistInstance:
    """One HamFist beam-search decoder tracking one CW signal via stdin.

    Identical channelization and duck-type interface as BmorseInstance.
    Accepts a wpm hint so it's ready for ML-estimated speed from Arc's WPM head.
    """

    def __init__(self, freq_offset, rf_khz, sample_rate, snr,
                 hamfist_bin='/home/fred/csdr-skimmer/research/HamFist/hamfist',
                 scp_path=None, wpm=30):
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

        # Same channelization as BmorseInstance — 4kHz mono at CW_TONE
        self.mix_freq = freq_offset - CW_TONE
        self.phase = 0.0
        self.dec_factor = sample_rate // BMORSE_RATE  # BMORSE_RATE = 4000

        from scipy.signal import firwin
        n_taps = self.dec_factor * 4 + 1
        self._fir_taps = firwin(n_taps, BMORSE_RATE / 2, fs=sample_rate)
        self._fir_state = np.zeros(len(self._fir_taps) - 1)
        self._dec_buf = np.zeros(0, dtype=np.float64)
        self._peak = 1.0

        cmd = [hamfist_bin, '-stdin',
               '-frq', str(CW_TONE),
               '-rate', str(BMORSE_RATE),
               '-spd', str(wpm if wpm > 0 else 30)]
        if scp_path:
            cmd += ['-scp', scp_path]
        self.process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )

    def feed_iq(self, i_samples, q_samples):
        """SSB channelize IQ to 4kHz and feed to hamfist stdin."""
        if not self.process or self.process.poll() is not None:
            return
        n = len(i_samples)
        if n == 0:
            return

        t = np.arange(n) / self.sample_rate
        phase_inc = 2 * np.pi * self.mix_freq
        phases = self.phase + phase_inc * t
        self.phase = (phases[-1] + phase_inc / self.sample_rate) % (2 * np.pi)

        mixed_real = i_samples * np.cos(phases) + q_samples * np.sin(phases)

        from scipy.signal import lfilter
        filtered, self._fir_state = lfilter(
            self._fir_taps, 1.0, mixed_real, zi=self._fir_state)

        self._dec_buf = np.concatenate([self._dec_buf, filtered])
        n_out = len(self._dec_buf) // self.dec_factor
        if n_out == 0:
            return

        usable = n_out * self.dec_factor
        decimated = self._dec_buf[:usable:self.dec_factor]
        self._dec_buf = self._dec_buf[usable:]

        peak = np.max(np.abs(decimated)) if len(decimated) > 0 else 0.0
        if peak > self._peak:
            self._peak = peak
        else:
            self._peak = 0.9999 * self._peak + 0.0001 * peak
        if self._peak > 0:
            decimated = decimated / self._peak * 0.8

        pcm = np.clip(decimated * 32767, -32767, 32767).astype(np.int16).tobytes()
        try:
            self.process.stdin.write(pcm)
        except (BrokenPipeError, OSError):
            pass

    def read(self):
        """Non-blocking read — passes through CALL: lines, strips plain text."""
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


class Channelizer:
    """SSB mix + IIR lowpass + decimate: IQ → mono PCM at output_rate.

    Optionally applies a post-decimation FIR bandpass around CW_TONE to
    reduce noise bandwidth. Used for bmorse (no internal narrow filter).
    NOT used for uhsdr (has internal 47 Hz Goertzel — FIR breaks its AGC).

    Shared across all decoder processes at the same frequency so the
    filter runs once per signal instead of once per decoder.
    """

    def __init__(self, freq_offset, input_rate, output_rate, normalize='fixed',
                 cw_fir_bw=0):
        self.mix_freq = freq_offset - CW_TONE
        self.phase = 0.0
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.dec_factor = input_rate // output_rate
        self.normalize = normalize  # 'fixed' for UHSDR, 'peak' for bmorse/HamFist

        self._pitch_detected = False
        self._pitch = CW_TONE
        self._pitch_buf = np.zeros(0, dtype=np.float64)

        from scipy.signal import butter, sosfilt_zi
        # IIR lowpass for anti-alias before decimation
        self._sos = butter(6, output_rate / 2, btype='low', fs=input_rate, output='sos')
        self._zi = sosfilt_zi(self._sos) * 0
        self._dec_buf = np.zeros(0, dtype=np.float64)
        self._peak = 0.0  # 0 so fast-attack triggers on first block; avoids ~2min ramp-up

        # Optional FIR bandpass (post-decimation, at output_rate)
        self._fir_taps = None
        self._fir_zi = None
        if cw_fir_bw > 0:
            from scipy.signal import firwin, lfilter_zi
            lo = max(50, CW_TONE - cw_fir_bw / 2)
            hi = min(output_rate / 2 - 50, CW_TONE + cw_fir_bw / 2)
            self._fir_taps = firwin(256, [lo, hi], fs=output_rate, pass_zero=False)
            self._fir_zi = lfilter_zi(self._fir_taps, 1.0) * 0

    @property
    def detected_pitch(self):
        return self._pitch

    def process(self, i_samples, q_samples):
        """Mix, filter, decimate IQ → int16 PCM bytes. Returns None if no output yet."""
        from scipy.signal import sosfilt
        n = len(i_samples)
        if n == 0:
            return None

        t = np.arange(n) / self.input_rate
        phase_inc = 2 * np.pi * self.mix_freq
        phases = self.phase + phase_inc * t
        self.phase = (phases[-1] + phase_inc / self.input_rate) % (2 * np.pi)

        mixed = i_samples * np.cos(phases) + q_samples * np.sin(phases)

        # IIR lowpass for anti-alias before decimation
        filtered, self._zi = sosfilt(self._sos, mixed, zi=self._zi)
        self._dec_buf = np.concatenate([self._dec_buf, filtered])

        n_out = len(self._dec_buf) // self.dec_factor
        if n_out == 0:
            return None

        usable = n_out * self.dec_factor
        decimated = self._dec_buf[:usable:self.dec_factor]
        self._dec_buf = self._dec_buf[usable:]

        # Auto pitch detection: accumulate ~15s, find actual CW tone
        if not self._pitch_detected:
            self._pitch_buf = np.concatenate([self._pitch_buf, decimated])
            needed = self.output_rate * 15
            if len(self._pitch_buf) >= needed:
                n_det = self.output_rate * 2
                spectrum = np.abs(np.fft.rfft(
                    self._pitch_buf[:n_det] * np.hanning(n_det)))
                freqs = np.fft.rfftfreq(n_det, 1.0 / self.output_rate)
                mask = (freqs >= 475) & (freqs <= 825)
                if np.any(mask):
                    peak_freq = freqs[mask][np.argmax(spectrum[mask])]
                    self._pitch = max(450, min(850, int(round(peak_freq))))
                    if abs(self._pitch - CW_TONE) > 5:
                        log.info("Auto pitch: %d Hz (expected %d Hz)",
                                 self._pitch, CW_TONE)
                self._pitch_detected = True
                self._pitch_buf = np.zeros(0)

        # Optional FIR bandpass (bmorse path only)
        if self._fir_taps is not None:
            from scipy.signal import lfilter
            decimated, self._fir_zi = lfilter(self._fir_taps, 1.0, decimated,
                                               zi=self._fir_zi)

        if self.normalize == 'peak':
            peak = np.max(np.abs(decimated)) if len(decimated) > 0 else 0.0
            if peak > self._peak:
                self._peak = peak
            else:
                self._peak = 0.9999 * self._peak + 0.0001 * peak
            if self._peak > 0:
                decimated = decimated / self._peak * 0.3
            pcm = np.clip(decimated * 32767, -32767, 32767).astype(np.int16).tobytes()
        else:  # fixed
            pcm = np.clip(decimated * 10.0, -32000, 32000).astype(np.int16).tobytes()

        return pcm


class _SubprocessDecoder:
    """Thin wrapper around a decoder subprocess — no channelization.

    Accepts pre-channelized PCM bytes from a shared Channelizer.
    """

    def __init__(self, rf_khz, snr, cmd, capture_wpm=False):
        self.rf_khz = rf_khz
        self.snr = snr
        self.decoded_text = ''
        self.total_chars = 0
        self.last_output = time.time()
        self.detected_wpm = 0  # WPM from decoder (if capture_wpm=True)
        self._capture_wpm = capture_wpm
        self.process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_wpm else subprocess.DEVNULL,
            bufsize=0,
        )

    def feed_pcm(self, pcm_bytes):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write(pcm_bytes)
            except (BrokenPipeError, OSError):
                pass

    def read(self):
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
        # Read WPM from stderr (non-blocking)
        if self._capture_wpm and self.process and self.process.stderr:
            while True:
                ready, _, _ = select.select([self.process.stderr], [], [], 0)
                if not ready:
                    break
                line = self.process.stderr.readline()
                if not line:
                    break
                text = line.decode('latin-1', errors='replace').strip()
                if text.startswith('WPM:'):
                    try:
                        self.detected_wpm = int(text[4:])
                    except ValueError:
                        pass
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


# Load uhsdr library once at module level
_uhsdr_lib = None
def _get_uhsdr_lib():
    global _uhsdr_lib
    if _uhsdr_lib is None:
        import ctypes as _ct
        try:
            _uhsdr_lib = _ct.CDLL('./libuhsdr_cw.so')
            _uhsdr_lib.uhsdr_init.restype = _ct.c_void_p
            _uhsdr_lib.uhsdr_init.argtypes = [_ct.c_float, _ct.c_float, _ct.c_int]
            _uhsdr_lib.uhsdr_feed.restype = _ct.c_int
            _uhsdr_lib.uhsdr_feed.argtypes = [_ct.c_void_p, _ct.POINTER(_ct.c_int16),
                                               _ct.c_int, _ct.c_char_p, _ct.c_int]
            _uhsdr_lib.uhsdr_get_wpm.restype = _ct.c_int
            _uhsdr_lib.uhsdr_get_wpm.argtypes = [_ct.c_void_p]
            _uhsdr_lib.uhsdr_free.restype = None
            _uhsdr_lib.uhsdr_free.argtypes = [_ct.c_void_p]
            log.info("Loaded libuhsdr_cw.so")
        except OSError:
            log.warning("libuhsdr_cw.so not found — falling back to subprocess decoders")
    return _uhsdr_lib


class _LibDecoder:
    """In-process CW decoder via libuhsdr_cw.so (ctypes).

    Same interface as _SubprocessDecoder: feed_pcm(), read(), kill().
    No subprocess, no pipe — direct function calls.
    """

    def __init__(self, rf_khz, snr, freq, sample_rate=12000, wpm=0):
        import ctypes as _ct
        self.rf_khz = rf_khz
        self.snr = snr
        self.decoded_text = ''
        self.total_chars = 0
        self.last_output = time.time()
        self.detected_wpm = 0
        self._outbuf = _ct.create_string_buffer(4096)
        self._pending = ''

        lib = _get_uhsdr_lib()
        self._lib = lib
        self._handle = lib.uhsdr_init(_ct.c_float(freq),
                                       _ct.c_float(sample_rate),
                                       _ct.c_int(wpm)) if lib else None

    def feed_pcm(self, pcm_bytes):
        if not self._handle:
            return
        import ctypes as _ct
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        if len(samples) == 0:
            return
        # Ensure contiguous memory
        if not samples.flags['C_CONTIGUOUS']:
            samples = np.ascontiguousarray(samples)
        n = self._lib.uhsdr_feed(
            self._handle,
            samples.ctypes.data_as(_ct.POINTER(_ct.c_int16)),
            len(samples),
            self._outbuf, 4096)
        if n > 0:
            wpm = self._lib.uhsdr_get_wpm(self._handle)
            if wpm > 0:
                self.detected_wpm = wpm
            chars = self._outbuf.value[:n].decode('latin-1', errors='replace')
            self._pending += chars
            self.decoded_text += chars
            self.total_chars += n
            self.last_output = time.time()

    def read(self):
        text = self._pending
        self._pending = ''
        return text

    def kill(self):
        if self._handle:
            self._lib.uhsdr_free(self._handle)
            self._handle = None


# Load bmorse library
_bmorse_lib = None
def _get_bmorse_lib():
    global _bmorse_lib
    if _bmorse_lib is None:
        import ctypes as _ct
        try:
            _bmorse_lib = _ct.CDLL('./libbmorse.so')
            _bmorse_lib.bmorse_create.restype = _ct.c_void_p
            _bmorse_lib.bmorse_create.argtypes = [_ct.c_float, _ct.c_float, _ct.c_int]
            _bmorse_lib.bmorse_feed.restype = _ct.c_int
            _bmorse_lib.bmorse_feed.argtypes = [_ct.c_void_p, _ct.POINTER(_ct.c_int16),
                                                 _ct.c_int, _ct.c_char_p, _ct.c_int]
            _bmorse_lib.bmorse_get_wpm.restype = _ct.c_int
            _bmorse_lib.bmorse_get_wpm.argtypes = [_ct.c_void_p]
            _bmorse_lib.bmorse_destroy.restype = None
            _bmorse_lib.bmorse_destroy.argtypes = [_ct.c_void_p]
            log.info("Loaded libbmorse.so")
        except OSError:
            pass  # bmorse library not available
    return _bmorse_lib


_ml_model = None
_ml_device = None

def _get_ml_model(model_path):
    """Load ML CTC model once, reuse across all signal instances."""
    global _ml_model, _ml_device
    if _ml_model is not None:
        return _ml_model, _ml_device
    try:
        import torch
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from train_model import CWDecoder
        _ml_device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        _ml_model = CWDecoder().to(_ml_device)
        ckpt = torch.load(model_path, map_location=_ml_device, weights_only=True)
        if isinstance(ckpt, dict) and 'model_state_dict' in ckpt:
            _ml_model.load_state_dict(ckpt['model_state_dict'], strict=False)
        else:
            _ml_model.load_state_dict(ckpt, strict=False)
        _ml_model.eval()
        log.info("ML decoder loaded: %s on %s", model_path, _ml_device)
    except Exception as e:
        log.warning("ML decoder failed to load: %s", e)
        _ml_model = None
    return _ml_model, _ml_device


class _MLDecoder:
    """In-process CNN+BiGRU+CTC decoder for CW signals.

    Accepts 16-bit PCM at BMORSE_RATE (4kHz), accumulates audio,
    runs CTC inference every ~6s, appends decoded text.
    Output tagged 'primary' — same weight as uhsdr in SpotTracker.
    """

    # 768 spectrogram frames × hop=32 samples = 24576 samples input,
    # plus fft_size=128 for the last frame window
    _WINDOW_SAMPLES = 768 * 32 + 128  # = 24704 samples (~6.18s at 4kHz)
    _HOP_SAMPLES = 384 * 32            # = 12288 samples (~3.07s hop)

    def __init__(self, rf_khz, snr, model_path, min_confidence=0.7):
        self.rf_khz = rf_khz
        self.snr = snr
        self.decoded_text = ''
        self.total_chars = 0
        self.last_output = time.time()
        self.detected_wpm = 0
        self._pending = ''
        self._min_confidence = min_confidence
        self._audio_buf = np.zeros(0, dtype=np.float32)
        self._model, self._device = _get_ml_model(model_path)

    def feed_pcm(self, pcm_bytes):
        if not self._model:
            return
        n = len(pcm_bytes) // 2
        if n == 0:
            return
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        self._audio_buf = np.concatenate([self._audio_buf, audio])

        while len(self._audio_buf) >= self._WINDOW_SAMPLES:
            chunk = self._audio_buf[:self._WINDOW_SAMPLES]
            self._audio_buf = self._audio_buf[self._HOP_SAMPLES:]
            text = self._decode_chunk(chunk)
            if text:
                self._pending += text + ' '
                self.decoded_text += text + ' '
                self.total_chars += len(text)
                self.last_output = time.time()

    def _decode_chunk(self, audio):
        try:
            import torch
            from train_model import compute_spectrogram, BLANK_IDX, IDX_TO_CHAR
            spec = compute_spectrogram(audio, fft_size=128, hop=32)
            # Pad/truncate to exactly 768 frames
            if spec.shape[0] < 768:
                spec = np.pad(spec, ((0, 768 - spec.shape[0]), (0, 0)))
            else:
                spec = spec[:768]
            tensor = torch.tensor(spec).unsqueeze(0).unsqueeze(0).to(self._device)
            with torch.no_grad():
                ctc_out, wpm_pred = self._model(tensor)
                wpm = wpm_pred[0].item()
                if wpm > 0:
                    self.detected_wpm = int(round(wpm))
                # Confidence-gated greedy decode — only emit chars above threshold
                probs = ctc_out[0].softmax(dim=-1).cpu()
                conf, indices = probs.max(dim=-1)
                decoded = []
                prev = BLANK_IDX
                for i in range(len(indices)):
                    idx = indices[i].item()
                    if idx != BLANK_IDX and idx != prev:
                        if conf[i].item() >= self._min_confidence:
                            decoded.append(IDX_TO_CHAR[idx])
                    prev = idx
                text = ''.join(decoded)
            return text.strip() if text else ''
        except Exception:
            return ''

    def read(self):
        text = self._pending
        self._pending = ''
        return text

    def kill(self):
        pass  # model singleton, not owned by this instance


class _LibBmorseDecoder:
    """In-process Bayesian CW decoder via libbmorse.so.

    Same interface as _LibDecoder. Used as second-pass fallback
    on signals uhsdr couldn't decode.
    """

    def __init__(self, rf_khz, snr, freq, sample_rate=4000, wpm=25):
        import ctypes as _ct
        self.rf_khz = rf_khz
        self.snr = snr
        self.decoded_text = ''
        self.total_chars = 0
        self.last_output = time.time()
        self.detected_wpm = 0
        self._outbuf = _ct.create_string_buffer(4096)
        self._pending = ''

        lib = _get_bmorse_lib()
        self._lib = lib
        self._handle = lib.bmorse_create(_ct.c_float(freq),
                                          _ct.c_float(sample_rate),
                                          _ct.c_int(wpm)) if lib else None

    def feed_pcm(self, pcm_bytes):
        if not self._handle:
            return
        import ctypes as _ct
        # bmorse library needs ctypes array, not numpy (segfaults with -O1+)
        n_samples = len(pcm_bytes) // 2
        if n_samples == 0:
            return
        SampArr = _ct.c_int16 * n_samples
        samples = SampArr.from_buffer_copy(pcm_bytes)
        n = self._lib.bmorse_feed(self._handle, samples, n_samples,
                                   self._outbuf, 4096)
        if n > 0:
            chars = self._outbuf.value[:n].decode('latin-1', errors='replace')
            self._pending += chars
            self.decoded_text += chars
            self.total_chars += n
            self.last_output = time.time()
            wpm = self._lib.bmorse_get_wpm(self._handle)
            if wpm > 0:
                self.detected_wpm = wpm

    def read(self):
        text = self._pending
        self._pending = ''
        return text

    def kill(self):
        if self._handle:
            self._lib.bmorse_destroy(self._handle)
            self._handle = None


# Load cw_engine library (C++ channelizer + dual decoder)
_cw_engine_lib = None
_cw_engine_initialized = False

def _get_cw_engine():
    global _cw_engine_lib, _cw_engine_initialized
    if _cw_engine_lib is not None:
        return _cw_engine_lib
    if _cw_engine_initialized:
        return None  # already tried, not available
    _cw_engine_initialized = True
    import ctypes as _ct
    try:
        _cw_engine_lib = _ct.CDLL('./libcw_engine.so')

        _cw_engine_lib.cw_engine_init.restype = _ct.c_int
        _cw_engine_lib.cw_engine_init.argtypes = [_ct.c_char_p]
        _cw_engine_lib.channel_create.restype = _ct.c_void_p
        _cw_engine_lib.channel_create.argtypes = [_ct.c_float, _ct.c_float]
        _cw_engine_lib.channel_feed_iq.restype = None
        _cw_engine_lib.channel_feed_iq.argtypes = [_ct.c_void_p,
            _ct.POINTER(_ct.c_float), _ct.POINTER(_ct.c_float), _ct.c_int]
        _cw_engine_lib.channel_decoder_count.restype = _ct.c_int
        _cw_engine_lib.channel_decoder_count.argtypes = [_ct.c_void_p]
        _cw_engine_lib.channel_read_text.restype = _ct.c_int
        _cw_engine_lib.channel_read_text.argtypes = [_ct.c_void_p, _ct.c_int,
            _ct.c_char_p, _ct.c_int, _ct.POINTER(_ct.c_int)]
        _cw_engine_lib.channel_decoder_speed.restype = _ct.c_int
        _cw_engine_lib.channel_decoder_speed.argtypes = [_ct.c_void_p, _ct.c_int]
        _cw_engine_lib.channel_get_pitch.restype = _ct.c_float
        _cw_engine_lib.channel_get_pitch.argtypes = [_ct.c_void_p]
        _cw_engine_lib.channel_get_wpm.restype = _ct.c_int
        _cw_engine_lib.channel_get_wpm.argtypes = [_ct.c_void_p]
        _cw_engine_lib.channel_destroy.restype = None
        _cw_engine_lib.channel_destroy.argtypes = [_ct.c_void_p]
        _cw_engine_lib.cw_engine_shutdown.restype = None

        ret = _cw_engine_lib.cw_engine_init(b"")
        if ret != 0:
            log.warning("cw_engine_init failed")
            _cw_engine_lib = None
            return None

        log.info("Loaded libcw_engine.so (C++ channelizer, text output mode)")
    except OSError:
        pass
    return _cw_engine_lib


class _CWEngineChannel:
    """Per-signal channel using C++ cw_engine (channelizer + multi-speed uhsdr).

    Feeds raw IQ to C++. Returns raw decoded text per decoder for SpotTracker.
    """

    def __init__(self, freq_offset, rf_khz, snr, sample_rate):
        import ctypes as _ct
        self.rf_khz = rf_khz
        self.snr = snr
        self.freq_offset = freq_offset
        self.total_chars = 0
        self.last_output = time.time()
        self.detected_wpm = 0

        self._sample_rate = sample_rate
        self._text_buf = _ct.create_string_buffer(8192)
        self._wpm_out = _ct.c_int(0)

        eng = _get_cw_engine()
        self._eng = eng
        self._handle = eng.channel_create(
            _ct.c_float(freq_offset), _ct.c_float(sample_rate)) if eng else None
        self._accumulated = {}  # decoder_idx → accumulated text

    def feed_iq(self, i_samples, q_samples):
        """Feed raw IQ — C++ does channelization + multi-speed decode."""
        if not self._handle or len(i_samples) == 0:
            return
        import ctypes as _ct
        i_f = np.ascontiguousarray(i_samples, dtype=np.float32)
        q_f = np.ascontiguousarray(q_samples, dtype=np.float32)
        self._eng.channel_feed_iq(
            self._handle,
            i_f.ctypes.data_as(_ct.POINTER(_ct.c_float)),
            q_f.ctypes.data_as(_ct.POINTER(_ct.c_float)),
            len(i_f))

    @property
    def decoder_count(self):
        if not self._handle:
            return 0
        return self._eng.channel_decoder_count(self._handle)

    def read_decoder_text(self, decoder_idx):
        """Read new text from a specific decoder. Returns (text, wpm, speed)."""
        if not self._handle:
            return '', 0, 0
        import ctypes as _ct
        n = self._eng.channel_read_text(
            self._handle, decoder_idx,
            self._text_buf, 8192, _ct.byref(self._wpm_out))
        if n > 0:
            text = self._text_buf.value[:n].decode('latin-1', errors='replace')
            self.total_chars += n
            self.last_output = time.time()
            self.detected_wpm = self._wpm_out.value
            speed = self._eng.channel_decoder_speed(self._handle, decoder_idx)
            return text, self._wpm_out.value, speed
        return '', 0, 0

    def kill(self):
        if self._handle:
            self._eng.channel_destroy(self._handle)
            self._handle = None


class SignalGroup:
    """One CW signal: shared channelization + all decoder processes.

    Runs one FIR filter per output rate (12kHz for UHSDR, 4kHz for
    bmorse/HamFist) instead of one per decoder process. Reduces FIR
    compute from N_decoders × N_samples × N_taps to
    2 × N_samples × N_taps per signal.
    """

    def __init__(self, freq_offset, rf_khz, sample_rate, snr,
                 decoder_bin, speeds,
                 bmorse_bin=None, hamfist_bin=None, hamfist_scp=None,
                 wpm=30, ml_model_path=None, ml_min_confidence=0.7):
        self.freq_offset = freq_offset
        self.rf_khz = rf_khz
        self.snr = snr
        self.last_seen = time.time()
        self.last_output = time.time()

        # Try C++ cw_engine first (owns channelization + both decoders)
        self._cw_engine = None
        eng = _get_cw_engine()
        if eng:
            self._cw_engine = _CWEngineChannel(freq_offset, rf_khz, snr, sample_rate)

        # Python channelizers — always create (runs in parallel with cw_engine)
        self._ch_uhsdr = Channelizer(freq_offset, sample_rate, DECODER_RATE,
                                     normalize='peak')
        self._ch_4k = Channelizer(freq_offset, sample_rate, BMORSE_RATE,
                                  normalize='peak',
                                  cw_fir_bw=400) if (bmorse_bin or hamfist_bin) else None

        # Two-pass decoder spawn:
        #   Immediately: start uhsdr at default pitch (600 Hz) — no buffering
        #   After pitch detection (~15s) + uhsdr WPM (or 10s timeout): start bmorse
        self._decoder_bin = decoder_bin
        self._speeds = speeds
        self._bmorse_bin = bmorse_bin
        self._hamfist_bin = hamfist_bin
        self._hamfist_scp = hamfist_scp
        self._wpm = wpm  # default WPM fallback
        self._bmorse_started = False
        self._bmorse_spawn_time = 0
        self._pcm_buffer_4k = b''

        # Second-pass bmorse fallback (spawned when uhsdr produces little)
        self._bmorse_fallback = None
        self._bmorse_fallback_time = 0  # when to check if uhsdr failed
        self._bmorse_fallback_started = False

        # Start Python uhsdr decoders — always (runs in parallel with cw_engine)
        self.decoders = []
        if True:
            lib = _get_uhsdr_lib()
            for spd in speeds:
                if lib:
                    dec = _LibDecoder(rf_khz, snr, freq=CW_TONE,
                                      sample_rate=DECODER_RATE, wpm=spd)
                else:
                    cmd = [decoder_bin, '-r', str(DECODER_RATE), '-f', str(CW_TONE)]
                    if spd > 0:
                        cmd += ['-s', str(spd)]
                    dec = _SubprocessDecoder(rf_khz, snr, cmd, capture_wpm=True)
                self.decoders.append(dec)

        self.bmorse = None
        self.hamfist = None
        self._ml_decoder = _MLDecoder(rf_khz, snr, ml_model_path, min_confidence=ml_min_confidence) if ml_model_path else None

    @property
    def _decoders_started(self):
        """Backwards compat — uhsdr starts immediately now."""
        return True

    def _start_bmorse(self, wpm, pitch):
        """Start bmorse/hamfist with detected WPM and pitch.
        Also respawn uhsdr at the detected pitch if it differs from CW_TONE."""
        # Respawn uhsdr at detected pitch (was started at CW_TONE initially)
        if pitch != CW_TONE:
            for d in self.decoders:
                d.kill()
            self.decoders = []
            lib = _get_uhsdr_lib()
            for spd in self._speeds:
                if lib:
                    dec = _LibDecoder(self.rf_khz, self.snr, freq=pitch,
                                      sample_rate=DECODER_RATE, wpm=spd)
                else:
                    cmd = [self._decoder_bin, '-r', str(DECODER_RATE), '-f', str(pitch)]
                    if spd > 0:
                        cmd += ['-s', str(spd)]
                    dec = _SubprocessDecoder(self.rf_khz, self.snr, cmd, capture_wpm=True)
                self.decoders.append(dec)
            log.info("Respawned uhsdr at pitch=%d Hz for %.1f kHz", pitch, self.rf_khz)

        if self._bmorse_bin:
            cmd = [self._bmorse_bin, '-stdin', '-txt',
                   '-spd', str(wpm), '-frq', str(pitch), '-rate', str(BMORSE_RATE)]
            self.bmorse = _SubprocessDecoder(self.rf_khz, self.snr, cmd)

        if self._hamfist_bin:
            cmd = [self._hamfist_bin, '-stdin', '-frq', str(pitch),
                   '-rate', str(BMORSE_RATE), '-spd', str(wpm)]
            if self._hamfist_scp:
                cmd += ['-scp', self._hamfist_scp]
            self.hamfist = _SubprocessDecoder(self.rf_khz, self.snr, cmd)

        # Feed buffered 4kHz PCM
        if self._pcm_buffer_4k:
            if self.bmorse:
                self.bmorse.feed_pcm(self._pcm_buffer_4k)
            if self.hamfist:
                self.hamfist.feed_pcm(self._pcm_buffer_4k)
        self._pcm_buffer_4k = b''

        self._bmorse_started = True
        log.info("bmorse spawned: pitch=%d Hz, spd=%d for %.1f kHz",
                 pitch, wpm, self.rf_khz)

    @property
    def count(self):
        engine_count = (self._cw_engine.decoder_count or 0) if self._cw_engine else 0
        pending_aux = ((1 if self._bmorse_bin else 0) + (1 if self._hamfist_bin else 0)) if not self._bmorse_started else 0
        return (engine_count + pending_aux + len(self.decoders)
                + (1 if self.bmorse else 0)
                + (1 if self.hamfist else 0))

    @property
    def total_chars(self):
        engine_chars = self._cw_engine.total_chars if self._cw_engine else 0
        return (engine_chars
                + sum(d.total_chars for d in self.decoders)
                + (self.bmorse.total_chars if self.bmorse else 0)
                + (self.hamfist.total_chars if self.hamfist else 0))

    @property
    def uhsdr_wpm(self):
        """WPM detected by uhsdr decoder (0 if not yet available)."""
        for d in self.decoders:
            if d.detected_wpm > 0:
                return d.detected_wpm
        return 0

    def feed_iq(self, i_samples, q_samples):
        # C++ cw_engine path: feed raw IQ (runs in parallel with Python)
        if self._cw_engine:
            self._cw_engine.feed_iq(i_samples, q_samples)

        # Python channelizer path (always runs)
        pcm_12k = self._ch_uhsdr.process(i_samples, q_samples)

        if self._ch_4k:
            pcm_4k = self._ch_4k.process(i_samples, q_samples)
        else:
            pcm_4k = None

        # uhsdr runs immediately — feed it always
        if pcm_12k:
            for d in self.decoders:
                d.feed_pcm(pcm_12k)

        # Two-pass: bmorse waits for pitch detection (~15s) + uhsdr WPM (or timeout)
        if not self._bmorse_started:
            if pcm_4k:
                self._pcm_buffer_4k += pcm_4k

            # Wait for pitch detection first
            pitch_ch = self._ch_4k if self._ch_4k else self._ch_uhsdr
            if not pitch_ch._pitch_detected:
                return

            # Pitch ready — start WPM timeout if not already set
            if self._bmorse_spawn_time == 0:
                self._bmorse_spawn_time = time.time() + 10

            pitch = pitch_ch.detected_pitch
            uhsdr_wpm = self.uhsdr_wpm
            if uhsdr_wpm > 0:
                self._start_bmorse(uhsdr_wpm, pitch)
            elif time.time() >= self._bmorse_spawn_time:
                self._start_bmorse(self._wpm, pitch)  # fallback
            return

        # Steady state: feed bmorse/hamfist (subprocess, if configured)
        if pcm_4k:
            if self.bmorse:
                self.bmorse.feed_pcm(pcm_4k)
            if self.hamfist:
                self.hamfist.feed_pcm(pcm_4k)

        # Second-pass bmorse fallback: spawn libbmorse when uhsdr hasn't decoded
        if not self._bmorse_fallback_started:
            if self._bmorse_fallback_time == 0 and self._bmorse_started:
                self._bmorse_fallback_time = time.time() + 20  # check after 20s
            if self._bmorse_fallback_time > 0 and time.time() >= self._bmorse_fallback_time:
                # Count REAL decoded chars (exclude [err] and bracket tags)
                real_chars = 0
                for d in self.decoders:
                    text = d.decoded_text
                    # Strip [err], [?], <xx> tags — count only real letters/digits/spaces
                    clean = re.sub(r'\[.*?\]|<.*?>', '', text)
                    real_chars += sum(1 for c in clean if c.isalnum())
                if real_chars < 20:  # uhsdr struggling on this signal
                    bmlib = _get_bmorse_lib()
                    if bmlib:
                        pitch_ch = self._ch_4k if self._ch_4k else self._ch_uhsdr
                        pitch = pitch_ch.detected_pitch if pitch_ch._pitch_detected else CW_TONE
                        wpm = self.uhsdr_wpm if self.uhsdr_wpm > 0 else 25
                        self._bmorse_fallback = _LibBmorseDecoder(
                            self.rf_khz, self.snr, freq=pitch,
                            sample_rate=BMORSE_RATE, wpm=wpm)
                        # Create lazy 4kHz channelizer for bmorse fallback
                        if self._ch_4k is None:
                            self._ch_4k = Channelizer(
                                self.freq_offset, self._ch_uhsdr.input_rate,
                                BMORSE_RATE, normalize='peak', cw_fir_bw=400)
                        log.info("bmorse fallback: pitch=%d wpm=%d for %.1f kHz (uhsdr had %d real chars)",
                                 pitch, wpm, self.rf_khz, real_chars)
                self._bmorse_fallback_started = True

        # Feed bmorse fallback if active
        if pcm_4k and self._bmorse_fallback:
            self._bmorse_fallback.feed_pcm(pcm_4k)

    def read_engine_spots(self):
        """Legacy — cw_engine now returns text via read(), SpotTracker handles matching."""
        return []

    def read(self):
        """Returns list of (rf_khz, snr, new_text, accumulated_text, dec_id, dec_type)."""
        results = []
        if self._cw_engine:
            # Read text from each C++ decoder → feed into SpotTracker
            for di in range(self._cw_engine.decoder_count):
                text, wpm, speed = self._cw_engine.read_decoder_text(di)
                if text:
                    # Accumulate per-decoder text for SpotTracker context
                    acc = self._cw_engine._accumulated
                    acc[di] = acc.get(di, '') + text
                    dec_type = 'primary'  # all uhsdr speeds are primary
                    dec_id = id(self._cw_engine) + di
                    results.append((self.rf_khz, self.snr, text, acc[di], dec_id, dec_type))
                    self.last_output = time.time()
            # Fall through to also collect Python decoder output
        for d in self.decoders:
            text = d.read()
            if text:
                # Suppress uhsdr output before pitch respawn — pre-respawn
                # output is noise from wrong pitch that generates false positives
                if not self._bmorse_started:
                    continue  # pitch not confirmed yet, discard
                results.append((self.rf_khz, self.snr, text, d.decoded_text, id(d), 'primary'))
                self.last_output = time.time()
        for d in ([self.bmorse] if self.bmorse else []) + \
                 ([self.hamfist] if self.hamfist else []) + \
                 ([self._bmorse_fallback] if self._bmorse_fallback else []):
            text = d.read()
            if text:
                results.append((self.rf_khz, self.snr, text, d.decoded_text, id(d), 'secondary'))
                self.last_output = time.time()
        return results

    def kill(self):
        if self._cw_engine:
            self._cw_engine.kill()
            self._cw_engine = None
        for d in self.decoders:
            d.kill()
        if self.bmorse:
            self.bmorse.kill()
        if self.hamfist:
            self.hamfist.kill()
        if self._bmorse_fallback:
            self._bmorse_fallback.kill()
            self._bmorse_fallback = None

    def all_processes(self):
        """Yield all subprocess decoders with (rf_khz, snr, process) tuples."""
        for d in self.decoders:
            yield self.rf_khz, self.snr, d
        if self.bmorse:
            yield self.rf_khz, self.snr, self.bmorse
        if self.hamfist:
            yield self.rf_khz, self.snr, self.hamfist


class InstanceManager:
    """Manages dynamic UHSDR decoder instances per detected signal.

    Multi-speed: spawns multiple decoder processes per signal at different
    WPM settings. Each gets the same channelized audio.
    """

    def __init__(self, sample_rate, decoder_bin='./uhsdr_cw',
                 max_instances=150, signal_timeout=90,
                 speeds=None, bmorse_bin=None, hamfist_bin=None,
                 hamfist_scp=None, ml_model_path=None, ml_min_confidence=0.7):
        self.sample_rate = sample_rate
        self.decoder_bin = decoder_bin
        self.bmorse_bin = bmorse_bin      # None = no bmorse
        self.hamfist_bin = hamfist_bin    # None = no HamFist
        self.hamfist_scp = hamfist_scp
        self.ml_model_path = ml_model_path  # None = no ML decoder
        self.ml_min_confidence = ml_min_confidence
        self.max_instances = max_instances
        self.signal_timeout = signal_timeout
        self.speeds = speeds or [0, 30]  # auto + 30 WPM
        # freq_key -> SignalGroup
        self.instances = {}
        self.center_khz = 0
        # WPM cache: freq_key -> last known WPM (survives signal eviction)
        self._wpm_cache = {}

    def update_signals(self, signals, center_khz, wpm_hint=0):
        """Update instance list based on detected signals.

        signals:  list of (offset_hz, snr_db) from FFT
        wpm_hint: ML-estimated WPM for this batch (0 = use default 30).
        """
        self.center_khz = center_khz
        now = time.time()

        # Mark existing groups as seen if signal still present
        for offset, snr in signals:
            key = int(round(offset / 100)) * 100
            if key in self.instances:
                self.instances[key].last_seen = now
                self.instances[key].snr = max(self.instances[key].snr, snr)

        spd = wpm_hint if wpm_hint > 0 else 25  # default 25 (contest CW center)

        # Spawn new SignalGroup for new signals
        for offset, snr in sorted(signals, key=lambda x: -x[1]):
            key = int(round(offset / 100)) * 100
            if key in self.instances:
                continue
            if abs(offset) < 100:  # skip DC
                continue
            if self.count >= self.max_instances:
                # Evict the lowest-SNR group if new signal is stronger
                if not self.instances:
                    break
                weakest_key = min(self.instances, key=lambda k: self.instances[k].snr)
                if snr <= self.instances[weakest_key].snr + 5:
                    break  # new signal not significantly stronger, stop
                evicted = self.instances.pop(weakest_key)
                # Cache WPM from evicted signal for future respawns
                evicted_wpm = evicted.uhsdr_wpm
                if evicted_wpm > 0:
                    self._wpm_cache[weakest_key] = evicted_wpm
                log.info("Evicted %.1f kHz (%+.0f dB) for %.1f kHz (%+.0f dB)",
                         center_khz + weakest_key/1000, evicted.snr,
                         center_khz + offset/1000, snr)
                evicted.kill()

            rf_khz = center_khz + offset / 1000
            # Use cached WPM if available, otherwise default
            cached_wpm = self._wpm_cache.get(key, 0)
            signal_wpm = cached_wpm if cached_wpm > 0 else spd
            group = SignalGroup(
                offset, rf_khz, self.sample_rate, snr,
                decoder_bin=self.decoder_bin,
                speeds=self.speeds,
                bmorse_bin=self.bmorse_bin,
                hamfist_bin=self.hamfist_bin,
                hamfist_scp=self.hamfist_scp,
                wpm=signal_wpm,
                ml_model_path=self.ml_model_path,
                ml_min_confidence=self.ml_min_confidence,
            )
            self.instances[key] = group
            extras = ('+bmorse' if self.bmorse_bin else '') + \
                     ('+hamfist' if self.hamfist_bin else '')
            log.info("Spawned %d decoders: %.1f kHz (offset %+.0f Hz, +%.0f dB, speeds %s%s)",
                     group.count, rf_khz, offset, snr,
                     [s if s > 0 else 'auto' for s in self.speeds], extras)

        # Kill groups when signal is truly gone
        dead = []
        for key, group in self.instances.items():
            last_activity = max(group.last_seen, group.last_output)
            if now - last_activity > self.signal_timeout:
                dead.append(key)
        for key in dead:
            group = self.instances.pop(key)
            # Cache WPM for future respawns
            dead_wpm = group.uhsdr_wpm
            if dead_wpm > 0:
                self._wpm_cache[key] = dead_wpm
            log.info("Killed %d decoders: %.1f kHz (%d chars total)",
                     group.count, group.rf_khz, group.total_chars)
            group.kill()

    def feed_all_iq(self, i_samples, q_samples):
        """Feed IQ to all SignalGroups — each runs its shared channelizer."""
        for group in list(self.instances.values()):
            group.feed_iq(i_samples, q_samples)

    def collect_all(self):
        """Read decoded text from all groups.

        Returns list of (rf_khz, snr, new_text, accumulated_text, dec_id, dec_type).
        """
        results = []
        for group in list(self.instances.values()):
            results.extend(group.read())
        return results

    def collect_engine_spots(self):
        """Read pre-validated spots from C++ cw_engine channels."""
        spots = []
        for group in list(self.instances.values()):
            spots.extend(group.read_engine_spots())
        return spots

    def kill_all(self):
        for group in self.instances.values():
            group.kill()
        self.instances.clear()

    @property
    def count(self):
        return sum(g.count for g in self.instances.values())


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
        # Per-frequency sighting counts: (call, freq_bin) → count
        self._freq_sightings = defaultdict(int)

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

    @staticmethod
    def _min_sightings(call):
        """Length-weighted sighting threshold: short calls need more confirmations."""
        n = len(call)
        if n <= 4:
            return 4  # 4-char fragments are mostly noise — require 4 sightings
        elif n == 5:
            return 3
        return 2  # 6+ chars: standard threshold

    def _can_respot(self, call, freq_khz, now):
        """Per-(call,freq) respot check. QSY >1kHz = immediate spot."""
        if not hasattr(self, '_respot_times'):
            self._respot_times = {}
        key = (call, round(freq_khz))
        last = self._respot_times.get(key, 0)
        return (now - last) >= self.respot_interval

    def _mark_spotted(self, call, freq_khz, now):
        if not hasattr(self, '_respot_times'):
            self._respot_times = {}
        key = (call, round(freq_khz))
        self._respot_times[key] = now

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

    def process(self, freq_khz, snr, text, context_text=None, decoder_id=0,
                dec_type='primary'):
        """Process decoded text. Returns list of spot dicts.

        text: new text fragment (1-2 chars in streaming mode)
        context_text: full accumulated text from the decoder instance
        dec_type: 'primary' (uhsdr) or 'secondary' (bmorse/hamfist).
            Secondary decoders only contribute at frequencies where no primary
            decoder has produced an exact SCP match.
        """
        # Track processed length per (frequency, decoder_id) to avoid re-processing.
        # decoder_id is id(d) from SignalGroup.read() — stable for the decoder's
        # lifetime, unique across concurrent decoders on the same frequency.
        freq_bin = int(round(freq_khz * 10))
        cache_key = (freq_bin, decoder_id)

        # Track which frequencies have primary exact matches
        if not hasattr(self, '_primary_matched'):
            self._primary_matched = set()  # freq_bins with primary exact SCP match

        # Skip secondary decoder output at frequencies where primary already matched
        if dec_type == 'secondary' and freq_bin in self._primary_matched:
            return []
        if not hasattr(self, '_processed_len'):
            self._processed_len = {}

        full_text = context_text or text
        prev_len = self._processed_len.get(cache_key, 0)

        # Only re-scan when we have 10+ new chars (avoid per-character overhead)
        if len(full_text) - prev_len < 10:
            return []

        self._processed_len[cache_key] = len(full_text)

        # Only process the NEW portion for fragment accumulation
        new_text = full_text[max(0, prev_len - 10):]  # overlap 10 chars for boundary
        clean = re.sub(r'\b[EIT]\b', '', new_text.upper())
        # Full text for context matching (CQ/TEST detection)
        context_clean = re.sub(r'\b[EIT]\b', '', full_text.upper())
        spots = []
        now = time.time()

        # --- Path 1: Exact SCP match ---
        # 1a: regex scan (word-boundary aware)
        seen_p1 = set()
        for m in CALL_RE.finditer(clean):
            call = m.group(1)
            if len(call) < 4 or call in FALSE_POSITIVES:
                continue
            if call in self.blacklist:
                continue

            if call in self.valid_calls:
                seen_p1.add(call)
                # Primary decoder exact match — suppress secondary decoders here
                if dec_type == 'primary':
                    self._primary_matched.add(freq_bin)
                    self._cycle_calls[call].add(freq_bin)
                info = self._tracking[call]
                info['count'] += 1
                info['freq'] = freq_khz
                info['snr'] = max(info['snr'], snr)

                has_context = bool(CQ_PATTERNS.search(context_clean))
                min_s = self._min_sightings(call)
                if (has_context or info['count'] >= min_s) and \
                   self._can_respot(call, freq_khz, now):
                    if len(self._cycle_calls[call]) < 3:  # hallucination check
                        self._mark_spotted(call, freq_khz, now)
                        spots.append({
                            'call': call,
                            'freq_khz': freq_khz,
                            'snr': snr,
                            'method': 'exact',
                        })

        # 1b: sliding window on collapsed text — catches calls embedded in
        # noise like "TUCY0S" where the regex word-boundary check misses them
        collapsed_new = re.sub(r'[^A-Z0-9]', '', clean)
        for wlen in range(4, 8):
            for i in range(len(collapsed_new) - wlen + 1):
                frag = collapsed_new[i:i+wlen]
                if frag in seen_p1 or frag in FALSE_POSITIVES or frag in self.blacklist:
                    continue
                if frag not in self.valid_calls:
                    continue
                seen_p1.add(frag)
                if dec_type == 'primary':
                    self._cycle_calls[frag].add(freq_bin)
                info = self._tracking[frag]
                info['count'] += 1
                info['freq'] = freq_khz
                info['snr'] = max(info['snr'], snr)

                min_s = self._min_sightings(frag)
                if info['count'] >= min_s and \
                   self._can_respot(frag, freq_khz, now):
                    if len(self._cycle_calls[frag]) < 3:
                        self._mark_spotted(frag, freq_khz, now)
                        spots.append({
                            'call': frag,
                            'freq_khz': freq_khz,
                            'snr': snr,
                            'method': 'exact_window',
                        })

        # --- Path 2: Fragment accumulation + fuzzy match ---
        # Secondary decoders (bmorse) skip fragment accumulation — their noise
        # output pollutes the accumulator and generates false positives.
        # They only contribute via Path 1 exact SCP matches above.
        if dec_type == 'secondary':
            return spots

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

                if avg_confidence < 0.70 or len(frag_list) < 3:
                    continue

                # Check consensus against SCP — exact first, then fuzzy
                if consensus_str in self.valid_calls:
                    info = self._tracking[consensus_str]
                    if self._can_respot(consensus_str, freq_khz, now):
                        if consensus_str not in self.blacklist:
                            self._mark_spotted(consensus_str, freq_khz, now)
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
                    if fuzzy and avg_confidence >= 0.90:
                        best_call, best_dist = min(fuzzy, key=lambda x: x[1])
                        info = self._tracking[best_call]
                        if self._can_respot(best_call, freq_khz, now):
                            if best_call not in self.blacklist:
                                self._mark_spotted(best_call, freq_khz, now)
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
        self._iq_buffer = deque()  # (i,q) tuples for FFT scan, O(1) append/trim
        self._feed_i = []          # numpy chunks for decoder feeding
        self._feed_q = []

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

        rx_sample_rate = self.cfg.get('sample_rate', 48000)
        sdr_port = self.cfg.get('sdr_port', 1024)

        devices = discover(port=sdr_port)
        if not devices:
            log.error("No HPSDR devices found")
            return False
        listen_port = self.cfg.get('hpsdr_listen_port', sdr_port)
        passive = self.cfg.get('passive', False)
        rx_filter = self.cfg.get('rx_filter', None)
        n_rx = self.cfg.get('max_receivers', 1)
        self.receiver = HPSDRReceiver(devices[0]['ip'], port=sdr_port,
                                      n_receivers=n_rx, sample_rate=rx_sample_rate,
                                      listen_port=listen_port,
                                      passive=passive, rx_filter=rx_filter)
        self.receiver.set_frequency(0, center)
        self.receiver.lna_gain = self.cfg.get('lna_gain', 20)

        speeds_cfg = self.cfg.get('decoder_speeds', [0, 25, 30, 35])
        self.manager = InstanceManager(
            sample_rate=rx_sample_rate,
            decoder_bin=self.cfg.get('decoder_bin', './uhsdr_cw'),
            max_instances=self.cfg.get('max_instances', 150),
            signal_timeout=self.cfg.get('signal_timeout', 90),
            speeds=speeds_cfg,
            bmorse_bin=self.cfg.get('bmorse_bin', None),
            hamfist_bin=self.cfg.get('hamfist_bin', None),
            hamfist_scp=self.cfg.get('hamfist_scp', None),
            ml_model_path=self.cfg.get('ml_model', None),
            ml_min_confidence=float(self.cfg.get('ml_min_confidence', 0.7)),
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
        """Called from HPSDR receiver thread — buffer only, no processing."""
        try:
            iq_arr = np.asarray(iq_samples, dtype=np.float64)
            i_chunk = iq_arr[:, 0] * 8388608.0
            q_chunk = iq_arr[:, 1] * 8388608.0
            with self._iq_lock:
                self._iq_buffer.extend(iq_samples)
                max_buf = self.cfg.get('sample_rate', 48000) * 10
                while len(self._iq_buffer) > max_buf:
                    self._iq_buffer.popleft()
                self._feed_i.append(i_chunk)
                self._feed_q.append(q_chunk)
        except Exception:
            pass  # Don't let errors kill the receiver thread

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

            # Drain feed buffer and push to decoders (200ms cap to stay real-time)
            # Cap CHUNKS before concatenate — avoids building huge arrays from backlogs
            with self._iq_lock:
                feed_i, feed_q = self._feed_i, self._feed_q
                self._feed_i, self._feed_q = [], []
            if feed_i:
                live_rate = self.cfg.get('sample_rate', 48000)
                chunk_size = len(feed_i[0]) if feed_i else 126
                max_chunks = max(1, live_rate // 5 // chunk_size)  # ~200ms of chunks
                if len(feed_i) > max_chunks:
                    feed_i = feed_i[-max_chunks:]
                    feed_q = feed_q[-max_chunks:]
                i_cat = np.concatenate(feed_i)
                q_cat = np.concatenate(feed_q)
                self.manager.feed_all_iq(i_cat, q_cat)

            # Periodic signal scan
            if now - last_scan >= scan_interval:
                last_scan = now
                self.tracker.reset_cycle()

                live_rate = self.cfg.get('sample_rate', 48000)
                fft_size = 8192
                needed = fft_size
                with self._iq_lock:
                    if len(self._iq_buffer) >= needed:
                        buf = list(itertools.islice(self._iq_buffer, len(self._iq_buffer) - needed, None))
                    else:
                        buf = None

                if buf is not None:
                    iq_arr = np.array([complex(i, q) for i, q in buf])
                    # Blackman window — critical for CW burst signals
                    # n_avg averaging without window destroys SNR for transient bursts
                    window = np.blackman(fft_size)
                    psd = np.abs(np.fft.fft(iq_arr * window)) ** 2
                    psd_db = 10 * np.log10(psd + 1e-20)
                    noise = np.median(psd_db)
                    min_snr = self.cfg.get('signal_min_snr', 12)

                    signals = []
                    N = fft_size
                    for i in range(1, N - 1):
                        if psd_db[i] > noise + min_snr and \
                           psd_db[i] > psd_db[i - 1] and psd_db[i] > psd_db[i + 1]:
                            delta = 0.5 * (psd_db[i-1] - psd_db[i+1]) / \
                                    (psd_db[i-1] - 2*psd_db[i] + psd_db[i+1])
                            exact = i + delta
                            if exact >= N / 2:
                                exact -= N
                            f = exact * live_rate / N
                            signals.append((f, psd_db[i] - noise))

                    # Cluster
                    clustered = []
                    for freq, snr in sorted(signals):
                        if not clustered or abs(freq - clustered[-1][0]) > 100:
                            clustered.append((freq, snr))
                        elif snr > clustered[-1][1]:
                            clustered[-1] = (freq, snr)

                    center_khz = self.receiver.frequencies[0] / 1000

                    if True:  # log every scan for debugging
                        log.info("noise=%.1f dB  thresh=%.1f dB  buf=%d samples",
                                 noise, noise + min_snr, len(self._iq_buffer))
                        top = sorted(signals, key=lambda x: -x[1])[:10]
                        for f, s in top:
                            log.info("  PEAK %.3f kHz %.1f dB", center_khz + f/1000, s)
                        if not top:
                            log.info("  (no peaks above threshold)")

                    cw_min = self.cfg.get('cw_min_khz', 0)
                    cw_max = self.cfg.get('cw_max_khz', 99999)
                    if cw_min or cw_max < 99999:
                        clustered = [(f, s) for f, s in clustered
                                     if cw_min <= center_khz + f/1000 <= cw_max]
                    self.manager.update_signals(clustered, center_khz)

            # Collect all decoder output (C++ engine + Python fallback)
            results = self.manager.collect_all()
            for rf_khz, snr, text, ctx, dec_id, dec_type in results:
                log.debug("DECODED %.1f kHz: %r", rf_khz, text[:120])
                # Skip pure noise until enough alphanumeric chars have accumulated
                # for a callsign to be present. Streaming decoders (fldigi_cw)
                # output one char at a time with spaces between — splitting never
                # yields multi-char tokens, so check collapsed alnum length instead.
                alnum = re.sub(r'[^A-Z0-9]', '', (ctx or text).upper())
                if len(alnum) < 4:
                    continue
                spots = self.tracker.process(rf_khz, snr, text, ctx, dec_id,
                                             dec_type=dec_type)
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
                total_chars = sum(
                    g.total_chars for g in self.manager.instances.values()
                )
                log.info("Status: %d spots, %d decoders, %d clients, %d chars, %.0fs",
                         self.spot_count, self.manager.count,
                         self.telnet.client_count, total_chars, elapsed)

            await asyncio.sleep(0.025)


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
        bmorse_bin=config.get('bmorse_bin'),
        ml_model_path=config.get('ml_model'),
        ml_min_confidence=float(config.get('ml_min_confidence', 0.7)),
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

    # Signal detection: scan the FULL recording once to find all signals
    # then feed continuous audio to each decoder for the entire duration.
    # This matches how eval works — one uninterrupted stream per signal.
    log.info("Scanning full recording for signals...")
    scan_dur = min(60, end_sec - start_sec)  # scan first 60s for signal detection
    if file_bits == 24:
        scan_i, scan_q = read_24bit_iq_chunk(args.file, start_sec, scan_dur, file_rate)
    else:
        import wave
        w = wave.open(args.file, 'rb')
        w.setpos(int(start_sec * file_rate))
        frames = w.readframes(int(scan_dur * file_rate))
        w.close()
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float64)
        scan_i = samples[0::2] if file_channels == 2 else samples
        scan_q = samples[1::2] if file_channels == 2 else np.zeros_like(samples)

    fft_size = 8192
    n_ffts = min(len(scan_i) // fft_size, 200)
    avg_spectrum = np.zeros(fft_size)
    for fi in range(n_ffts):
        chunk = scan_i[fi*fft_size:(fi+1)*fft_size] + \
                1j * scan_q[fi*fft_size:(fi+1)*fft_size]
        avg_spectrum += np.abs(np.fft.fft(chunk * np.hanning(fft_size))) ** 2
    avg_spectrum /= max(n_ffts, 1)
    avg_db = 10 * np.log10(avg_spectrum + 1e-20)
    freqs = np.fft.fftfreq(fft_size, 1.0 / file_rate)
    noise = np.median(avg_db)
    min_snr = config.get('signal_min_snr', 8)

    signals = []
    for i in range(1, fft_size - 1):
        if avg_db[i] > noise + min_snr and \
           avg_db[i] > avg_db[i-1] and avg_db[i] > avg_db[i+1]:
            signals.append((freqs[i], avg_db[i] - noise))

    clustered = []
    for freq, snr in sorted(signals):
        if not clustered or abs(freq - clustered[-1][0]) > 200:
            clustered.append((freq, snr))
        elif snr > clustered[-1][1]:
            clustered[-1] = (freq, snr)

    log.info("  %d signals detected — spawning decoders once", len(clustered))
    manager.update_signals(clustered, center_khz)
    del scan_i, scan_q

    # Feed continuous audio chunks — same decoder instances for entire recording
    for t_start in np.arange(start_sec, end_sec, chunk_sec):
        t_end = min(t_start + chunk_sec, end_sec)
        dur = t_end - t_start
        log.info("Feeding %.0f-%.0fs (%.1f-%.1f min)...",
                 t_start, t_end, t_start/60, t_end/60)

        if file_bits == 24:
            i_data, q_data = read_24bit_iq_chunk(args.file, t_start, dur, file_rate)
        else:
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
            for rf_khz, snr, text, ctx, dec_id, dec_type in results:
                total_chars += len(text)
                total_results += 1
                spots = tracker.process(rf_khz, snr, text, ctx, dec_id,
                                        dec_type=dec_type)
                for spot in spots:
                    all_spots.append(spot)
                    log.info("SPOT: %.1f kHz %s %d dB [%s]",
                             spot['freq_khz'], spot['call'],
                             spot['snr'], spot['method'])

        # Final collect after all data fed — give decoders a moment to flush
        import time as _time
        _time.sleep(0.5)
        results = manager.collect_all()
        for rf_khz, snr, text, ctx, dec_id, dec_type in results:
            total_chars += len(text)
            total_results += 1
            spots = tracker.process(rf_khz, snr, text, ctx, dec_id,
                                    dec_type=dec_type)
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
        for inst_rf, inst_snr, inst in group.all_processes():
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
                    if call not in decoded_calls or inst_snr > decoded_calls[call][1]:
                        decoded_calls[call] = (inst_rf, inst_snr, text[:80])
            # Method 2: sliding window — catches calls embedded in
            # noise like "TUCY0S" where regex finds "UCY0S" instead
            collapsed = re.sub(r'[^A-Z0-9]', '', text)
            for wlen in range(4, 8):
                for i in range(len(collapsed) - wlen + 1):
                    frag = collapsed[i:i+wlen]
                    if frag in calls and frag not in FALSE_POS_EVAL:
                        if frag not in decoded_calls or inst_snr > decoded_calls[frag][1]:
                            decoded_calls[frag] = (inst_rf, inst_snr, text[:80])

            # Method 3: fuzzy SCP — fragment NOT in SCP but edit distance 1
            # from an SCP call. Catches AD4EB→AD4UB, K0II→K0IS etc.
            for m in CALL_RE_EVAL.finditer(text):
                frag = m.group(0)
                if len(frag) < 4 or frag in FALSE_POS_EVAL or frag in calls:
                    continue
                # Not in SCP — try edit distance 1 match
                # Build candidates by substituting each character
                for pos in range(len(frag)):
                    for ch in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789':
                        if ch == frag[pos]:
                            continue
                        candidate = frag[:pos] + ch + frag[pos+1:]
                        if candidate in calls and candidate not in FALSE_POS_EVAL:
                            if candidate not in decoded_calls or inst_snr > decoded_calls[candidate][1]:
                                decoded_calls[candidate] = (inst_rf, inst_snr, text[:80])
                            break
                    else:
                        continue
                    break

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
