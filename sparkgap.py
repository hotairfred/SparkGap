#!/usr/bin/env python3
"""
sparkgap.py — SparkGap live CW skimmer daemon.

Streaming architecture with dynamic decoder instances:
    1. Continuous IQ stream from Red Pitaya via HPSDR Protocol 1
    2. Periodic FFT signal detection (every 5 seconds)
    3. Per-signal SSB channelization → UHSDR decoder instances
    4. Multi-speed decoding (auto + fixed WPM) per signal
    5. Decoded text collected, validated against MASTER.SCP
    6. Spots served on DX cluster telnet port

Usage:
    python3 sparkgap.py
    python3 sparkgap.py --config skimmer.json
    python3 sparkgap.py --file B1_recording.wav --start-min 15 --end-min 30
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
import socket
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
from flex_iq import FlexIQReceiver
from telnet_server import SpotTelnetServer

log = logging.getLogger('sparkgap')

# ---------------------------------------------------------------------------
# Fast C receiver for HPSDR Protocol 1 (multi-band)
# ---------------------------------------------------------------------------
_hpsdr_fast_lib = None
def _get_hpsdr_fast():
    global _hpsdr_fast_lib
    if _hpsdr_fast_lib is None:
        import ctypes as _ct
        try:
            lib = _ct.CDLL('./libhpsdr_fast.so')
            lib.hpsdr_create.restype = _ct.c_void_p
            lib.hpsdr_create.argtypes = [_ct.c_char_p, _ct.c_int, _ct.c_int,
                                          _ct.c_int, _ct.c_int]
            lib.hpsdr_set_freq.restype = None
            lib.hpsdr_set_freq.argtypes = [_ct.c_void_p, _ct.c_int, _ct.c_uint32]
            lib.hpsdr_start.restype = None
            lib.hpsdr_start.argtypes = [_ct.c_void_p]
            lib.hpsdr_stop.restype = None
            lib.hpsdr_stop.argtypes = [_ct.c_void_p]
            lib.hpsdr_destroy.restype = None
            lib.hpsdr_destroy.argtypes = [_ct.c_void_p]
            lib.hpsdr_drain.restype = _ct.c_int
            lib.hpsdr_drain.argtypes = [_ct.c_void_p, _ct.c_int,
                                         _ct.POINTER(_ct.c_double),
                                         _ct.POINTER(_ct.c_double), _ct.c_int]
            lib.hpsdr_available.restype = _ct.c_int
            lib.hpsdr_available.argtypes = [_ct.c_void_p, _ct.c_int]
            lib.hpsdr_pkt_count.restype = _ct.c_uint64
            lib.hpsdr_pkt_count.argtypes = [_ct.c_void_p]
            lib.hpsdr_drop_count.restype = _ct.c_uint64
            lib.hpsdr_drop_count.argtypes = [_ct.c_void_p]
            lib.hpsdr_n_receivers.restype = _ct.c_int
            lib.hpsdr_n_receivers.argtypes = [_ct.c_void_p]
            lib.hpsdr_drain_to_scanner.restype = _ct.c_int
            lib.hpsdr_drain_to_scanner.argtypes = [
                _ct.c_void_p, _ct.c_int, _ct.c_void_p, _ct.c_double,
                _ct.c_void_p]  # feed_fn pointer
            lib.hpsdr_set_scanner.restype = None
            lib.hpsdr_set_scanner.argtypes = [
                _ct.c_void_p, _ct.c_int, _ct.c_void_p,
                _ct.c_void_p, _ct.c_double]
            lib.hpsdr_set_decode.restype = None
            lib.hpsdr_set_decode.argtypes = [
                _ct.c_void_p, _ct.c_void_p, _ct.c_int]
            # Per-RX decode setup — used to selectively skip C decode for
            # certain RX (e.g. PFB scanners with different struct layout
            # and envelope rate from per-bin ITILA scanner).  Optional;
            # falls back gracefully if libhpsdr_fast doesn't export it.
            try:
                lib.hpsdr_set_rx_decode.restype = None
                lib.hpsdr_set_rx_decode.argtypes = [
                    _ct.c_void_p, _ct.c_int, _ct.c_void_p, _ct.c_int]
            except AttributeError:
                pass
            lib.hpsdr_start_worker.restype = None
            lib.hpsdr_start_worker.argtypes = [_ct.c_void_p]
            lib.hpsdr_stop_worker.restype = None
            lib.hpsdr_stop_worker.argtypes = [_ct.c_void_p]
            lib.hpsdr_set_ft8.restype = None
            lib.hpsdr_set_ft8.argtypes = [
                _ct.c_void_p, _ct.c_int, _ct.c_double,
                _ct.c_double, _ct.c_char_p]
            lib.hpsdr_poll_results.restype = _ct.c_int
            lib.hpsdr_poll_results.argtypes = [
                _ct.c_void_p, _ct.c_void_p, _ct.c_int]
            lib.hpsdr_enable_ft8.restype = None
            lib.hpsdr_enable_ft8.argtypes = [
                _ct.c_void_p, _ct.c_int, _ct.c_double, _ct.c_double]
            lib.hpsdr_ft8_swap_read.restype = _ct.c_int
            lib.hpsdr_ft8_swap_read.argtypes = [
                _ct.c_void_p, _ct.c_int,
                _ct.POINTER(_ct.c_float), _ct.POINTER(_ct.c_float),
                _ct.c_int, _ct.POINTER(_ct.c_double)]
            try:
                lib.hpsdr_iq_snapshot_read.restype = _ct.c_int
                lib.hpsdr_iq_snapshot_read.argtypes = [
                    _ct.c_void_p, _ct.c_int,
                    _ct.POINTER(_ct.c_float), _ct.POINTER(_ct.c_float),
                    _ct.c_int, _ct.POINTER(_ct.c_double)]
            except AttributeError:
                pass
            try:
                lib.hpsdr_pkt_lost.restype = _ct.c_uint64
                lib.hpsdr_pkt_lost.argtypes = [_ct.c_void_p]
            except AttributeError:
                pass
            _hpsdr_fast_lib = lib
            log.info("Loaded libhpsdr_fast.so (C receiver)")
        except OSError:
            log.warning("libhpsdr_fast.so not found — falling back to Python receiver")
            _hpsdr_fast_lib = False
    return _hpsdr_fast_lib if _hpsdr_fast_lib else None


class _CReceiver:
    """Drop-in replacement for HPSDRReceiver using C receive thread."""

    def __init__(self, ip, port, n_receivers, sample_rate, lna_gain=20):
        import ctypes as _ct
        self.lib = _get_hpsdr_fast()
        self.n_receivers = n_receivers
        self.sample_rate = sample_rate
        self._h = None
        if self.lib:
            self._h = _ct.c_void_p(self.lib.hpsdr_create(
                ip.encode(), port, n_receivers, sample_rate, lna_gain))
            if not self._h:
                log.error("hpsdr_create failed")

    def set_frequency(self, rx_index, freq_hz):
        if self._h and self.lib:
            self.lib.hpsdr_set_freq(self._h, rx_index, int(freq_hz))

    def start(self):
        if self._h and self.lib:
            self.lib.hpsdr_start(self._h)
            log.info("C receiver started: %d receivers at %d Hz",
                     self.n_receivers, self.sample_rate)

    def stop(self):
        if self._h and self.lib:
            self.lib.hpsdr_stop(self._h)
            log.info("C receiver stopped (pkts=%d drops=%d)",
                     self.lib.hpsdr_pkt_count(self._h),
                     self.lib.hpsdr_drop_count(self._h))

    def drain(self, rx_index, max_n):
        """Drain up to max_n IQ samples for receiver rx_index.
        Returns (i_array, q_array) as numpy float64."""
        import ctypes as _ct
        if not self._h or not self.lib:
            return np.zeros(0), np.zeros(0)
        i_buf = np.empty(max_n, dtype=np.float64)
        q_buf = np.empty(max_n, dtype=np.float64)
        n = self.lib.hpsdr_drain(self._h, rx_index,
                                  i_buf.ctypes.data_as(_ct.POINTER(_ct.c_double)),
                                  q_buf.ctypes.data_as(_ct.POINTER(_ct.c_double)),
                                  max_n)
        return i_buf[:n], q_buf[:n]

    def available(self, rx_index):
        if not self._h or not self.lib:
            return 0
        return self.lib.hpsdr_available(self._h, rx_index)

    def drain_to_scanner(self, rx_index, scanner_sc):
        """Drain IQ and feed directly to ITILA scanner in C — zero Python overhead."""
        if not self._h or not self.lib or not scanner_sc or not scanner_sc._h:
            return 0
        import ctypes as _ct
        # Get raw function pointer from the SAME library Python loaded
        feed_ptr = _ct.cast(scanner_sc._lib.itila_sc_feed_iq,
                            _ct.c_void_p)
        return self.lib.hpsdr_drain_to_scanner(
            self._h, rx_index,
            scanner_sc._h, _ct.c_double(8388608.0),
            feed_ptr)

    def destroy(self):
        if self._h and self.lib:
            self.lib.hpsdr_destroy(self._h)
            self._h = None

CALL_RE = re.compile(
    r'(?<![A-Z0-9])'
    r'('
    r'[A-Z0-9]{1,3}/[A-Z0-9]{1,2}\d{1,2}[A-Z]{1,4}'   # PREFIX/CALL: PJ2/AG3I, VE3/WF8Z
    r'|'
    r'[A-Z0-9]{1,2}\d{1,2}[A-Z]{1,4}(?:/[A-Z0-9]{1,4})?'  # CALL or CALL/SUFFIX: W1AW/0, K9MA/P
    r')'
    r'(?![A-Z0-9])'
)
FALSE_POSITIVES = {
    'CQ', 'TEST', 'QRZ', 'DE', 'TU', '5NN', '599', 'RST',
    'QSL', 'QTH', 'QRL', 'CFM', 'PSE', 'TNX', 'TKS',
    'BT', 'AR', 'SK', 'KN', 'AS', 'EE5E', 'TT5T',
}
CQ_PATTERNS = re.compile(r'\b(CQ|TEST|QRZ|QRL|CWT|SST|MST|FD|SS|NA|UP)\b', re.IGNORECASE)

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
    add_calls = set()
    if add_path and os.path.exists(add_path):
        with open(add_path) as f:
            for line in f:
                line = line.strip().upper()
                if line:
                    add_calls.add(line)
    calls |= add_calls
    blacklist = set()
    if blacklist_path and os.path.exists(blacklist_path):
        with open(blacklist_path) as f:
            for line in f:
                # Strip end-of-line comments and surrounding whitespace,
                # then skip empty lines and full-line comments.
                line = line.split('#', 1)[0].strip().upper()
                if line:
                    blacklist.add(line)
    log.info("Database: %d calls (%d add_calls) + %d blacklisted",
             len(calls), len(add_calls), len(blacklist))
    return calls, blacklist, add_calls


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
        self._secondary_pitches = []  # co-channel peaks detected alongside primary
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

    @property
    def secondary_pitches(self):
        return self._secondary_pitches

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
                    spec_m = spectrum[mask]
                    freqs_m = freqs[mask]
                    peak_idx = np.argmax(spec_m)
                    peak_freq = freqs_m[peak_idx]
                    peak_amp = spec_m[peak_idx]
                    self._pitch = max(450, min(850, int(round(peak_freq))))
                    if abs(self._pitch - CW_TONE) > 5:
                        log.info("Auto pitch: %d Hz (expected %d Hz)",
                                 self._pitch, CW_TONE)
                    # Secondary pitch: local maxima > 15% of primary, > 50 Hz away
                    threshold = peak_amp * 0.15
                    candidates = []
                    for i in range(len(freqs_m)):
                        if abs(freqs_m[i] - peak_freq) <= 50:
                            continue
                        if spec_m[i] < threshold:
                            continue
                        lo, hi = max(0, i - 5), min(len(spec_m), i + 6)
                        if spec_m[i] == np.max(spec_m[lo:hi]):
                            candidates.append((spec_m[i], int(round(freqs_m[i]))))
                    candidates.sort(reverse=True)  # strongest first
                    # Deduplicate: merge candidates within 50 Hz of each other
                    merged = []
                    for amp, freq in candidates:
                        if not any(abs(freq - mf) < 50 for _, mf in merged):
                            merged.append((amp, freq))
                    self._secondary_pitches = [
                        max(450, min(850, f)) for _, f in merged[:2]
                    ]
                    if self._secondary_pitches:
                        log.info("Secondary pitches: %s Hz alongside primary %d Hz",
                                 self._secondary_pitches, self._pitch)
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


class PFBChannelizer:
    """Polyphase filter bank: full-band IQ → N narrowband channels at DECODER_RATE.

    N_CHAN=768 channels, 250 Hz spacing, oversample=48 → 12 kHz output rate.
    Two CW signals ≥250 Hz apart in RF frequency land in separate bins with
    ≤-60 dB bleedthrough from the prototype Kaiser filter.

    Shared across all SignalGroups in an InstanceManager.  Call process() once
    per IQ block; the result (stored in last_output) is read by PFBChannel.
    """

    N_CHAN = 384
    OVERSAMPLE = 48          # N/M — output rate = input_rate * os / N = 12000
    TAPS_PER_CHAN = 9        # polyphase branch length; 9 → ~60 dB stopband

    def __init__(self, input_rate=192000):
        from scipy.signal import firwin
        self.input_rate = input_rate
        self.N = self.N_CHAN
        self.M = self.N_CHAN // self.OVERSAMPLE   # = 16 input samples per output step
        self.output_rate = input_rate * self.OVERSAMPLE // self.N_CHAN  # = 12000
        self.bin_spacing = float(input_rate) / self.N_CHAN              # = 250.0 Hz

        # Prototype lowpass filter: cutoff = half channel bandwidth
        n_taps = self.N_CHAN * self.TAPS_PER_CHAN
        h = firwin(n_taps, 1.0 / (2 * self.N_CHAN), window=('kaiser', 10.0))
        # Unity-gain normalization: scale so PFB output amplitude matches input.
        # sqrt(N)/K gives passband gain ≈ 1, matching Channelizer amplitude.
        h = (h * np.sqrt(self.N_CHAN) / self.TAPS_PER_CHAN).astype(np.float64)
        # Polyphase matrix: H[k, j] = h[k + j*N] → shape (N_CHAN, TAPS_PER_CHAN)
        self._H = h.reshape(self.TAPS_PER_CHAN, self.N_CHAN).T.copy()

        # Rolling history buffer (newest-first), length N*K
        # Branch n reads hist[n::N][:K] = hist.reshape(K,N)[:, n]
        self._hist = np.zeros(self.N_CHAN * self.TAPS_PER_CHAN, dtype=np.complex128)
        self._buf = np.zeros(0, dtype=np.complex128)

        # Per-bin baseband correction: out[k,s] *= exp(-j*2π*k*M*s/N)
        # Without this, bin k output sits at f0 (mod output_rate), not at residual.
        # phase_inc[k] = -2π * k * M / N  (radians per output step, per bin)
        N, M = self.N_CHAN, self.N_CHAN // self.OVERSAMPLE
        self._phase_inc = (-2 * np.pi * np.arange(N, dtype=np.float64) * M / N)
        self._phase_vec = np.zeros(N, dtype=np.float64)  # running phase per bin

        # Most recent processed output — read by PFBChannel.process()
        self.last_output = None   # shape (N_CHAN, n_steps) or None

    def process(self, i_samples, q_samples):
        """Ingest one IQ block → update last_output.  Returns (N_CHAN, n_steps) or None."""
        x = (i_samples + 1j * q_samples).astype(np.complex128)
        self._buf = np.concatenate([self._buf, x])

        M, N, K = self.M, self.N, self.TAPS_PER_CHAN
        n_steps = len(self._buf) // M
        if n_steps == 0:
            self.last_output = None
            return None

        usable = n_steps * M
        block = self._buf[:usable]
        self._buf = self._buf[usable:]

        # Build newest-first extended sequence: [block reversed, prior history]
        # full_rev[i] = the i-th most recent sample overall
        full_rev = np.concatenate([block[::-1], self._hist])  # (n_steps*M + N*K,)

        # Strided view: sig[l, n, s'] = full_rev[s'*M + l*N + n]
        # s'=0 → newest chunk (step n_steps-1); s'=n_steps-1 → oldest chunk (step 0)
        itemsize = full_rev.itemsize
        sig = np.lib.stride_tricks.as_strided(
            full_rev,
            shape=(K, N, n_steps),
            strides=(N * itemsize, itemsize, M * itemsize),
        )

        # Polyphase filter — y_rev[n, s'] = Σ_l H[n,l] * sig[l,n,s']
        y_rev = np.einsum('nl,lns->ns', self._H, sig)   # (N, n_steps)

        # s'=0 is NEWEST step; reverse so out[:, 0] = oldest step
        out = np.fft.ifft(y_rev[:, ::-1], axis=0) * N   # (N, n_steps)

        # Per-bin baseband correction: out[k,s] *= exp(-j*2π*k*M*s/N)
        # This mixes each bin to baseband (removes bin-centre carrier).
        s_vec = np.arange(n_steps, dtype=np.float64)
        phase_matrix = self._phase_vec[:, np.newaxis] + np.outer(self._phase_inc, s_vec)
        out *= np.exp(1j * phase_matrix)
        self._phase_vec = (self._phase_vec + self._phase_inc * n_steps) % (2 * np.pi)

        # Update history: newest N*K samples
        self._hist[:] = full_rev[:N * K]

        self.last_output = out
        return out

    def channel_powers_db(self):
        """Per-channel mean power in dB from last_output.  Used for signal detection."""
        if self.last_output is None:
            return None
        power = np.mean(np.abs(self.last_output) ** 2, axis=1)
        return 10.0 * np.log10(power + 1e-20)

    def freq_to_bin(self, freq_offset_hz):
        """Map signed Hz offset from receiver centre → bin index [0, N_CHAN)."""
        return int(round(freq_offset_hz / self.bin_spacing)) % self.N

    def bin_to_freq(self, k):
        """Map bin index → signed Hz offset."""
        f = k * self.bin_spacing
        if f > self.input_rate / 2:
            f -= self.input_rate
        return f


class PFBChannel:
    """Extracts one channel from the shared PFBChannelizer output → int16 PCM.

    Duck-type compatible with Channelizer: exposes the same process(),
    detected_pitch, secondary_pitches, new_secondary_pitches, and
    _pitch_detected attributes used by SignalGroup.
    """

    def __init__(self, freq_offset, pfb, output_rate=None, normalize='peak',
                 cw_fir_bw=0):
        self._pfb = pfb
        self.freq_offset = freq_offset
        self.output_rate = output_rate or pfb.output_rate
        self.normalize = normalize

        self.bin_idx = pfb.freq_to_bin(freq_offset)
        self.bin_centre = pfb.bin_to_freq(self.bin_idx)
        # Residual: signal lands at (CW_TONE - residual) Hz in this channel
        self.residual_hz = freq_offset - self.bin_centre
        self._shift_hz = CW_TONE - self.residual_hz
        self._phase = 0.0  # running phase for continuous frequency shift

        # Pitch detection — same state as Channelizer
        self._pitch_detected = False
        self._pitch = CW_TONE
        self._secondary_pitches = []
        self._new_secondary_pitches = []
        self._known_sec_pitches = set()
        self._pitch_buf = np.zeros(0, dtype=np.float64)

        self._peak = 0.0

        # Optional post-decimation FIR bandpass (for bmorse 4kHz path).
        # Do NOT use on uhsdr path — adds group delay that breaks timing.
        self._fir_taps = None
        self._fir_zi = None
        if cw_fir_bw > 0 and self.output_rate > 0:
            from scipy.signal import firwin, lfilter_zi
            lo = max(50, CW_TONE - cw_fir_bw / 2)
            hi = min(self.output_rate / 2 - 50, CW_TONE + cw_fir_bw / 2)
            self._fir_taps = firwin(256, [lo, hi], fs=self.output_rate,
                                    pass_zero=False)
            self._fir_zi = lfilter_zi(self._fir_taps, 1.0) * 0

    @property
    def detected_pitch(self):
        return self._pitch

    @property
    def secondary_pitches(self):
        return self._secondary_pitches

    @property
    def new_secondary_pitches(self):
        pitches = self._new_secondary_pitches[:]
        self._new_secondary_pitches = []
        return pitches

    def process(self, i_samples, q_samples):
        """Same signature as Channelizer.process().  Reads from pfb.last_output."""
        pfb_out = self._pfb.last_output
        if pfb_out is None or pfb_out.shape[1] == 0:
            return None

        # Extract this channel
        ch = pfb_out[self.bin_idx]  # (n_steps,) complex at pfb.output_rate
        pfb_rate = self._pfb.output_rate

        # Frequency-shift so CW tone lands at CW_TONE Hz (time vector at pfb rate)
        n = len(ch)
        t = np.arange(n) / pfb_rate
        phase_end = self._phase + 2 * np.pi * self._shift_hz * n / pfb_rate
        phases = self._phase + 2 * np.pi * self._shift_hz * t
        self._phase = phase_end % (2 * np.pi)
        audio = (ch * np.exp(1j * phases)).real.astype(np.float64)

        # Decimate to self.output_rate if needed (e.g. 12 kHz → 4 kHz for bmorse)
        dec = pfb_rate // self.output_rate
        if dec > 1:
            audio = audio[::dec]

        # Pitch detection
        if not self._pitch_detected:
            self._pitch_buf = np.concatenate([self._pitch_buf, audio])
            needed = self.output_rate * 15
            if len(self._pitch_buf) >= needed:
                n_det = self.output_rate * 2
                spectrum = np.abs(np.fft.rfft(
                    self._pitch_buf[:n_det] * np.hanning(n_det)))
                freqs = np.fft.rfftfreq(n_det, 1.0 / self.output_rate)
                mask = (freqs >= 475) & (freqs <= 825)
                if np.any(mask):
                    spec_m = spectrum[mask]
                    freqs_m = freqs[mask]
                    peak_idx = np.argmax(spec_m)
                    peak_freq = freqs_m[peak_idx]
                    peak_amp = spec_m[peak_idx]
                    self._pitch = max(450, min(850, int(round(peak_freq))))
                    if abs(self._pitch - CW_TONE) > 5:
                        log.info("PFB auto pitch: %d Hz (expected %d Hz)",
                                 self._pitch, CW_TONE)
                    threshold = peak_amp * 0.15
                    candidates = []
                    for i in range(len(freqs_m)):
                        if abs(freqs_m[i] - peak_freq) <= 25:
                            continue
                        if spec_m[i] < threshold:
                            continue
                        lo, hi = max(0, i - 5), min(len(spec_m), i + 6)
                        if spec_m[i] == np.max(spec_m[lo:hi]):
                            candidates.append((spec_m[i], int(round(freqs_m[i]))))
                    candidates.sort(reverse=True)
                    merged = []
                    for amp, freq in candidates:
                        if not any(abs(freq - mf) < 25 for _, mf in merged):
                            merged.append((amp, freq))
                    self._secondary_pitches = [
                        max(450, min(850, f)) for _, f in merged[:2]
                    ]
                    self._known_sec_pitches = set(self._secondary_pitches)
                    if self._secondary_pitches:
                        log.info("PFB secondary pitches: %s Hz alongside primary %d Hz",
                                 self._secondary_pitches, self._pitch)
                self._pitch_detected = True
                self._pitch_buf = np.zeros(0, dtype=np.float64)

        # Optional post-dec FIR bandpass
        if self._fir_taps is not None:
            from scipy.signal import lfilter
            audio, self._fir_zi = lfilter(self._fir_taps, 1.0, audio, zi=self._fir_zi)

        # Normalise → int16 PCM
        if self.normalize == 'peak':
            peak = np.max(np.abs(audio)) if len(audio) > 0 else 0.0
            if peak > self._peak:
                self._peak = peak
            else:
                self._peak = 0.9999 * self._peak + 0.0001 * peak
            if self._peak > 0:
                audio = audio / self._peak * 0.3
            return np.clip(audio * 32767, -32767, 32767).astype(np.int16).tobytes()
        else:
            return np.clip(audio * 10.0, -32000, 32000).astype(np.int16).tobytes()


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
        self._spawn_wpm   = wpm   # WPM this instance was created with (0=auto)
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


# ---------------------------------------------------------------------------
# libitila.so — Bayesian CW decoder (envelope in, callsigns out)
# ---------------------------------------------------------------------------

_ITILA_CQ_WORDS = {'CQ', 'TEST', 'CWT', 'SST', 'MST', 'FD', 'SS', 'NA', 'UP'}
# QRZ/QRL deliberately NOT runner anchors. After a QSO the runner sends
# "TU CALL 5NN QRZ?" and the next decode chunk often starts with the next
# caller — extracting after QRZ grabs the wrong station. CQ + contest tokens
# uniquely identify runners; we lose nothing real by dropping QRZ here.
# Base callsign: 1-2 prefix letters, 1-2 digits, 1-4 suffix letters
_BASE_CALL_PAT = re.compile(r'^[A-Z]{1,2}[0-9]{1,4}[A-Z]{1,6}$')
# Slash suffixes that don't make it a new full callsign: /P /M /MM /QRP /0-9
_SLASH_SUFFIX_PAT = re.compile(r'^([0-9]|P|M|MM|QRP|A|B)$')

def _is_base_call(tok):
    # 99%+ of real base callsigns are 4-7 chars; 8+ are almost all slash calls
    # which Case 1 in _itila_extract_cq_call handles separately.  An 8+ char
    # match here is almost always trailing decoder garbage glommed onto a real
    # call (e.g. HB9AMO → "HB9AMOHBM").
    #
    # 3-char minimum — 205 SCP calls are 3 chars (M7Z, M3A, G6M, M2G, M0X,
    # G3X, G8X — common UK/EU contest 1x1 calls).  Live RF (2026-04-26 07:00)
    # caught M7Z calling CQ at 7018.6 with raw text containing "M7Z" 4+ times
    # across windows, but the previous 4-char floor blocked extraction.
    return bool(_BASE_CALL_PAT.match(tok)) and 3 <= len(tok) <= 7

def _itila_extract_all_calls(text, min_count=2):
    """Extract callsigns that appear at least min_count times in raw decoded text.

    Used for context extraction when a CQ was seen on this bin recently.
    Requires repetition to filter noise — real calls get repeated in QSOs.
    Returns list of unique callsign strings, most-repeated first.
    """
    tokens = re.findall(r'[A-Z0-9]{4,10}', text.upper())
    from collections import Counter
    counts = Counter()
    for tok in tokens:
        if _is_base_call(tok) and tok not in FALSE_POSITIVES:
            counts[tok] += 1
    return [call for call, cnt in counts.most_common() if cnt >= min_count]


def _itila_extract_cq_call(text, valid_calls=None):
    """Extract callsign adjacent to CQ/TEST in decoded Morse text.

    Handles full slash callsigns in all forms:
      CALL/SUFFIX  — W1AW/0, K9MA/P, K9MA/MM, K9MA/QRP
      PREFIX/CALL  — PJ2/AG3I, VE3/WF8Z
      Split tokens — slash decoded as space: ['PJ2', 'AG3I'] → PJ2/AG3I

    `valid_calls` (set of SCP callsigns) is optional but strongly recommended
    for 3-char-extraction correctness: when both M7Z (real) and M7G (noise)
    appear as candidates, picking the SCP-valid one beats picking by recency.

    Returns callsign string (with slash) or None.
    """
    tokens = re.findall(r'[A-Z0-9]+(?:/[A-Z0-9]+)*', text.upper())

    # Split on DE boundary: "EC7RDE" → "EC7R", "DE"; "EC7RDEN1MX" → "EC7R", "DE", "N1MX"
    # Only split when the part before DE looks like a callsign (letter+digit pattern)
    split_de = []
    for tok in tokens:
        m = re.match(r'^([A-Z]{1,2}\d[A-Z0-9]*?)DE([A-Z0-9].*)?$', tok)
        if m and m.group(1) and len(m.group(1)) >= 4:
            split_de.append(m.group(1))
            split_de.append('DE')
            if m.group(2):
                split_de.append(m.group(2))
        else:
            split_de.append(tok)
    tokens = split_de

    # Split merged CQ+callsign tokens (e.g. "CQCWTWJ9B" → "CQ", "CWT", "WJ9B")
    _SPLIT_PREFIXES = ('CWT', 'CQ', 'TEST', 'MST', 'SST', 'QRZ')
    expanded = []
    for tok in tokens:
        remaining = tok
        while remaining:
            matched = False
            for prefix in _SPLIT_PREFIXES:
                if remaining.startswith(prefix) and len(remaining) > len(prefix):
                    tail = remaining[len(prefix):]
                    if re.match(r'[A-Z]{1,2}\d', tail):
                        expanded.append(prefix)
                        expanded.append(tail)
                        remaining = ''
                        matched = True
                        break
                    else:
                        expanded.append(prefix)
                        remaining = tail
                        matched = True
                        break
            if not matched:
                expanded.append(remaining)
                break
    tokens = expanded

    # Collect ALL callsign candidates from each CQ window.
    # Returning the most-frequent (last among ties) beats grabbing the first
    # because ITILA sometimes garbles the first occurrence of a repeated
    # callsign (e.g. "CQ CQ E E A2JD K2JD K" → prefer K2JD over A2JD).
    candidates = []

    # Fuzzy CQ trigger matching: allow 1-char substitution (FWT→CWT, TES→TEST, CWE→CWT)
    _FUZZY_CQ = {'CQ', 'CWT', 'TEST', 'SST', 'MST', 'FD', 'SS', 'NA', 'UP'}
    def _is_cq_trigger(tok):
        if tok in _ITILA_CQ_WORDS:
            return True
        if len(tok) < 2 or len(tok) > 5:
            return False
        for cq in _FUZZY_CQ:
            if len(tok) == len(cq) and sum(a != b for a, b in zip(tok, cq)) == 1:
                return True
            if len(tok) == len(cq) - 1 and cq.startswith(tok):
                return True
        return False

    for i, tok in enumerate(tokens):
        if not _is_cq_trigger(tok):
            continue
        # 5 tokens after CQ trigger — wide enough for "CQ NA NA E HZ1TT" pattern
        # but narrow enough to block answering stations (6+ tokens out)
        for j in range(i + 1, min(i + 6, len(tokens))):
            t = tokens[j]

            # Case 1: already a slash call in one token
            if '/' in t:
                parts = t.split('/', 1)
                left, right = parts[0], parts[1]
                if _is_base_call(left) and right:
                    candidates.append(t if len(right) <= 4 else left)
                    break  # unambiguous — stop scanning this CQ window
                if _is_base_call(right) and re.match(r'^[A-Z]{1,2}[0-9]', left):
                    candidates.append(t)
                    break
                for part in (right, left):
                    if _is_base_call(part):
                        candidates.append(part)
                        break
                break

            # Case 2: plain base call — continue scanning (don't break) so we
            # collect both occurrences when callsign is repeated after garble
            if _is_base_call(t):
                if j + 1 < min(i + 6, len(tokens)):
                    nxt = tokens[j + 1]
                    if _SLASH_SUFFIX_PAT.match(nxt):
                        candidates.append(f'{t}/{nxt}')
                        break  # slash suffix found — unambiguous
                candidates.append(t)
                continue  # keep scanning for possible second clean copy


            # Case 3: short DX prefix (e.g. PJ2, VE3) — slash decoded as space
            if re.match(r'^[A-Z]{1,2}[0-9]$', t) and j + 1 < min(i + 6, len(tokens)):
                nxt = tokens[j + 1]
                if _is_base_call(nxt):
                    candidates.append(f'{t}/{nxt}')
                    break

    if not candidates:
        return None

    # If we have a callsign DB, strongly prefer SCP-valid candidates over
    # noise candidates of the same shape.  Necessary now that 3-char calls
    # are allowed: real call M7Z and noise call M7G both pass the regex,
    # and old recency-based tie-break was picking M7G half the time.
    from collections import Counter
    if valid_calls is not None:
        scp_cands = [c for c in candidates if c in valid_calls]
        if scp_cands:
            counts = Counter(scp_cands)
            max_count = max(counts.values())
            for c in reversed(scp_cands):
                if counts[c] == max_count:
                    return c

    # Fallback: most frequent candidate; break ties by last occurrence.
    counts = Counter(candidates)
    max_count = max(counts.values())
    for c in reversed(candidates):
        if counts[c] == max_count:
            return c
    return candidates[-1]


_itila_lib = None

def _get_itila_lib():
    global _itila_lib
    if _itila_lib is None:
        import ctypes as _ct
        try:
            _itila_lib = _ct.CDLL('./libitila.so')
            _itila_lib.itila_create.restype = _ct.c_void_p
            _itila_lib.itila_create.argtypes = [_ct.c_int, _ct.c_double]
            _itila_lib.itila_feed.restype  = _ct.c_char_p
            _itila_lib.itila_feed.argtypes = [_ct.c_void_p,
                                               _ct.POINTER(_ct.c_double),
                                               _ct.c_int,
                                               _ct.c_double, _ct.c_double]
            _itila_lib.itila_free.restype  = None
            _itila_lib.itila_free.argtypes = [_ct.c_void_p]
            _itila_lib.itila_get_wpm.restype  = _ct.c_double
            _itila_lib.itila_get_wpm.argtypes = [_ct.c_void_p]
            log.info("Loaded libitila.so")
        except OSError:
            log.warning("libitila.so not found — ITILA decoder unavailable")
            _itila_lib = False  # sentinel: tried and failed
    return _itila_lib if _itila_lib else None


_rtty_lib = None

def _get_rtty_lib():
    global _rtty_lib
    if _rtty_lib is None:
        import ctypes as _ct
        try:
            _rtty_lib = _ct.CDLL('./librtty.so')
            _rtty_lib.rtty_create.restype = _ct.c_void_p
            _rtty_lib.rtty_create.argtypes = [_ct.c_int, _ct.c_double]
            _rtty_lib.rtty_feed.restype  = _ct.c_char_p
            _rtty_lib.rtty_feed.argtypes = [_ct.c_void_p,
                                             _ct.POINTER(_ct.c_double),
                                             _ct.c_int,
                                             _ct.POINTER(_ct.c_double)]
            _rtty_lib.rtty_free.restype  = None
            _rtty_lib.rtty_free.argtypes = [_ct.c_void_p]
            log.info("Loaded librtty.so")
        except OSError:
            log.warning("librtty.so not found — RTTY decoder unavailable")
            _rtty_lib = False
    return _rtty_lib if _rtty_lib else None


def _rtty_calls_match(a, b):
    """Two RTTY-decoded callsigns are likely the same station if either
    is a substring of the other (catches truncations like KD7N ⊂ KD7ND
    and prefix garbage like K0MK ⊂ ITK0MK), OR they differ by at most
    one edit (catches single-bit-error substitutions like KD7ND ↔ KD7NB).
    Used by RTTY confirmation to collapse decoder bit-error variants."""
    if a == b:
        return True
    if a in b or b in a:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    # len(b) == len(a) + 1: try removing each char from b
    for i in range(len(b)):
        if a == b[:i] + b[i+1:]:
            return True
    return False


# RTTY contest sub-bands per band, as offset (Hz) from band center.
# Centers in our config are 3590/7090/14090/21090/28090 kHz.
RTTY_RANGES = {
    3590:  (-10000,  +10000),  # 80m: 3580-3600
    7090:  (-55000,  -35000),  # 40m: 7035-7055 (RTTY contest portion)
    14090: (-10000,  +10000),  # 20m: 14080-14100
    21090: (-10000,  +10000),  # 15m: 21080-21100
    28090: (-10000,  +10000),  # 10m: 28080-28100
}


def _rtty_scan_band(skimmer, rtty_lib, bn, band_center_khz, fi, fq, n_samples):
    """Scan one band's IQ snapshot for RTTY signals; broadcast spots.

    MVP piggyback on the FT8 minute snapshot. Detects pairs of spectral
    peaks ~170 Hz apart (the RTTY shift) within the band's RTTY sub-range,
    demodulates each candidate to USB audio at 12 kHz with the signal
    centered at +1000 Hz, and feeds librtty's decoder. Returns spot count.
    """
    import ctypes as _ct
    from scipy.signal import resample_poly, find_peaks
    from numpy.fft import fft, ifft

    rng = RTTY_RANGES.get(int(band_center_khz))
    if rng is None:
        return 0
    rtty_lo, rtty_hi = rng

    band_center_hz = band_center_khz * 1000.0
    iq = fi.astype(np.float64) + 1j * fq.astype(np.float64)

    # Peak detection: 2-second FFT → 0.5 Hz bins
    fft_n = 2 * 192000
    if len(iq) < fft_n:
        return 0
    spec = np.abs(fft(iq[:fft_n]))
    freqs = np.fft.fftfreq(fft_n, 1.0 / 192000)
    order = np.argsort(freqs)
    freqs_s = freqs[order]
    spec_s = spec[order]

    sel = (freqs_s >= rtty_lo) & (freqs_s <= rtty_hi)
    sub_freqs = freqs_s[sel]
    sub_spec = spec_s[sel]
    if len(sub_spec) < 200:
        return 0

    # Smooth (~5 Hz) and find peaks above 4× median
    smoothed = np.convolve(sub_spec, np.ones(11) / 11, mode='same')
    noise = float(np.median(smoothed))
    if noise <= 0:
        return 0
    bin_hz = float(sub_freqs[1] - sub_freqs[0])
    peaks_idx, _ = find_peaks(smoothed, height=noise * 4,
                              distance=max(1, int(60 / bin_hz)))
    if len(peaks_idx) < 2:
        return 0

    # Pair peaks ~170 Hz apart (within ±20 Hz)
    candidates = []  # (rf_hz, snr_db)
    seen_keys = set()
    for ii, pi in enumerate(peaks_idx):
        for pj in peaks_idx[ii + 1:]:
            d = float(sub_freqs[pj] - sub_freqs[pi])
            if d > 200:  # past the shift; subsequent js are further
                break
            if abs(d - 170.0) <= 20.0:
                center_off = float(sub_freqs[pi] + sub_freqs[pj]) / 2.0
                rf_hz = band_center_hz + center_off
                key = int(round(rf_hz / 100)) * 100
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                amp = float(smoothed[pi] + smoothed[pj]) / 2.0
                snr_db = 20.0 * np.log10(amp / noise)
                candidates.append((rf_hz, snr_db))
                break

    n_peaks = len(peaks_idx)
    if not candidates:
        log.info("RTTY %s scan: %d peaks, 0 paired (noise=%.0f, range %d..%d Hz)",
                 bn, n_peaks, noise, int(rtty_lo), int(rtty_hi))
        return 0
    candidates.sort(key=lambda x: -x[1])
    candidates = candidates[:10]
    log.info("RTTY %s scan: %d peaks → %d paired candidates (top SNR %.0f dB)",
             bn, n_peaks, len(candidates), candidates[0][1])

    # Decode each candidate
    n_spots = 0
    seen_calls = set()
    audio_center = 1000.0
    t = np.arange(n_samples) / 192000.0
    for rf_hz, snr_db in candidates:
        offset_hz = (rf_hz - band_center_hz) - audio_center
        mixed = iq * np.exp(-1j * 2.0 * np.pi * offset_hz * t)
        dec12 = resample_poly(mixed, 1, 16)
        sp = fft(dec12)
        sp[len(sp) // 2:] = 0
        audio = ifft(sp).real * 2.0
        peak = float(np.max(np.abs(audio)))
        if peak <= 0:
            continue
        audio = (audio / peak * 0.7).astype(np.float64)

        h = rtty_lib.rtty_create(_ct.c_int(12000), _ct.c_double(audio_center))
        if not h:
            continue
        confidence = _ct.c_double(0.0)
        try:
            txt_ptr = rtty_lib.rtty_feed(
                h,
                audio.ctypes.data_as(_ct.POINTER(_ct.c_double)),
                _ct.c_int(len(audio)),
                _ct.byref(confidence))
            text = txt_ptr.decode('latin-1', errors='replace') if txt_ptr else ''
        finally:
            rtty_lib.rtty_free(h)

        text = text.strip()
        if not text:
            log.info("RTTY %s @ %.1f kHz (%.0f dB): no text", bn, rf_hz/1000.0, snr_db)
            continue
        log.info("RTTY %s @ %.1f kHz (%.0f dB): %s",
                 bn, rf_hz / 1000.0, snr_db, text[:80])

        # Letter-first prefix: kills "33W"-style garbage from bit errors,
        # keeps 1x1 special event calls (K0M, K1Y, W2B). Allows 3-8 chars.
        for call in re.findall(r'\b[A-Z][A-Z0-9]{0,2}[0-9][A-Z]{1,4}\b', text):
            if not (3 <= len(call) <= 7):
                continue
            if call in seen_calls:
                continue
            seen_calls.add(call)

            # Multi-cycle confirmation: only spot when same call decoded
            # ≥2 times within 5 min at the same freq (200 Hz bucket).
            # Suppress re-emit for 10 min after spotting. Decoder bit errors
            # produce different garbled spellings each cycle — real stations
            # repeat themselves on the same freq.
            #
            # Fuzzy matching: a new call merges with an existing pending
            # entry in the same freq bucket if they're substring-related
            # or within edit-distance 1 (KD7ND/KD7NB/KD7N/KD7NDR all
            # collapse to whichever was seen first). Catches the
            # "ITK0MK / K0MK" smushed-call false positive too.
            now = time.time()
            if not hasattr(skimmer, '_rtty_pending'):
                skimmer._rtty_pending = {}   # key -> [timestamps]
                skimmer._rtty_emitted = {}   # key -> last_emit_ts
            freq_bucket = round(rf_hz / 200.0) * 200.0

            # Look for an existing variant of this call in the same bucket
            canonical = call
            for (existing_call, existing_bucket) in list(skimmer._rtty_pending.keys()):
                if existing_bucket == freq_bucket and \
                        _rtty_calls_match(call, existing_call):
                    canonical = existing_call
                    break
            # Also check emitted (recently spotted) — same-bucket variant
            # debounce should suppress this even under a different spelling
            for (existing_call, existing_bucket) in list(skimmer._rtty_emitted.keys()):
                if existing_bucket == freq_bucket and \
                        _rtty_calls_match(call, existing_call):
                    canonical = existing_call
                    break
            key = (canonical, freq_bucket)

            last_emit = skimmer._rtty_emitted.get(key, 0)
            if now - last_emit < 600:
                continue  # debounce: already spotted recently (any variant)

            sightings = [t for t in skimmer._rtty_pending.get(key, [])
                         if now - t < 300]
            sightings.append(now)
            skimmer._rtty_pending[key] = sightings

            # High-SNR shortcut: skip 2-cycle confirmation if signal is
            # strong (≥25 dB). Real loud signals very rarely produce
            # bit-error garbage that just happens to match a callsign
            # regex; weak signals are where the false positives come from.
            strong = snr_db >= 25.0

            if not strong and len(sightings) < 2:
                log.info("RTTY pending: %s @ %.1f kHz (%d/2 sightings)",
                         call, rf_hz / 1000.0, len(sightings))
                continue

            skimmer._rtty_emitted[key] = now
            log.info("*** RTTY SPOT: %.1f kHz  %-10s  %+d dB  [%s] ***",
                     rf_hz / 1000.0, call, int(snr_db), text[:50])
            skimmer.telnet.broadcast_spot(
                freq_khz=rf_hz / 1000.0,
                dx_call=call,
                snr=int(snr_db),
                mode='RTTY',
                comment=text[:60])
            skimmer.spot_count += 1
            n_spots += 1

    return n_spots


class _ItilaDsp:
    """Python wrapper around libitila_dsp.so — scanner DSP core."""

    def __init__(self, lib, handle):
        self._lib = lib
        self._h   = handle

    def add_bin(self, f_hz):
        return self._lib.itila_dsp_add_bin(self._h, f_hz)

    def remove_bin(self, f_hz):
        self._lib.itila_dsp_remove_bin(self._h, f_hz)

    def feed(self, i_arr, q_arr):
        import ctypes as _ct
        n = len(i_arr)
        ip = i_arr.ctypes.data_as(_ct.POINTER(_ct.c_double))
        qp = q_arr.ctypes.data_as(_ct.POINTER(_ct.c_double))
        self._lib.itila_dsp_feed(self._h, ip, qp, _ct.c_int(n))

    def env_n(self, f_hz):
        return self._lib.itila_dsp_env_n(self._h, f_hz)

    def drain_env(self, f_hz, env100, env200, max_n):
        import ctypes as _ct
        p100 = env100.ctypes.data_as(_ct.POINTER(_ct.c_double))
        p200 = env200.ctypes.data_as(_ct.POINTER(_ct.c_double))
        return self._lib.itila_dsp_drain_env(
            self._h, _ct.c_double(f_hz), p100, p200, _ct.c_int(max_n))

    def free(self):
        self._lib.itila_dsp_free(self._h)
        self._h = None


def _get_itila_dsp(sample_rate, center_hz, max_bins, sos100, sos200):
    import ctypes as _ct
    try:
        lib = _ct.CDLL('./libitila_dsp.so')
        lib.itila_dsp_create.restype  = _ct.c_void_p
        lib.itila_dsp_create.argtypes = [
            _ct.c_int, _ct.c_double, _ct.c_int,
            _ct.POINTER(_ct.c_double), _ct.c_int,
            _ct.POINTER(_ct.c_double)]
        lib.itila_dsp_free.restype    = None
        lib.itila_dsp_free.argtypes   = [_ct.c_void_p]
        lib.itila_dsp_add_bin.restype    = _ct.c_int
        lib.itila_dsp_add_bin.argtypes   = [_ct.c_void_p, _ct.c_double]
        lib.itila_dsp_remove_bin.restype  = None
        lib.itila_dsp_remove_bin.argtypes = [_ct.c_void_p, _ct.c_double]
        lib.itila_dsp_feed.restype    = None
        lib.itila_dsp_feed.argtypes   = [
            _ct.c_void_p,
            _ct.POINTER(_ct.c_double), _ct.POINTER(_ct.c_double), _ct.c_int]
        lib.itila_dsp_env_n.restype    = _ct.c_int
        lib.itila_dsp_env_n.argtypes   = [_ct.c_void_p, _ct.c_double]
        lib.itila_dsp_drain_env.restype  = _ct.c_int
        lib.itila_dsp_drain_env.argtypes = [
            _ct.c_void_p, _ct.c_double,
            _ct.POINTER(_ct.c_double), _ct.POINTER(_ct.c_double), _ct.c_int]

        n_sos = sos100.shape[0]
        s100  = np.ascontiguousarray(sos100, dtype=np.float64)
        s200  = np.ascontiguousarray(sos200, dtype=np.float64)
        p100  = s100.ctypes.data_as(_ct.POINTER(_ct.c_double))
        p200  = s200.ctypes.data_as(_ct.POINTER(_ct.c_double))
        h = lib.itila_dsp_create(
            _ct.c_int(sample_rate), _ct.c_double(center_hz),
            _ct.c_int(max_bins), p100, _ct.c_int(n_sos), p200)
        if not h:
            log.warning("itila_dsp_create returned NULL")
            return None
        log.info("Loaded libitila_dsp.so (sample_rate=%d center=%.1f Hz max_bins=%d)",
                 sample_rate, center_hz, max_bins)
        return _ItilaDsp(lib, _ct.c_void_p(h))
    except OSError:
        log.warning("libitila_dsp.so not found — falling back to Python DSP")
        return None


class _ItilaSc:
    """Python wrapper around libitila_scanner.so."""

    def __init__(self, lib, handle):
        self._lib = lib
        self._h   = handle
        self._max_bins = 128

    def feed_iq(self, i_arr, q_arr):
        import ctypes as _ct
        n = len(i_arr)
        ip = i_arr.ctypes.data_as(_ct.POINTER(_ct.c_double))
        qp = q_arr.ctypes.data_as(_ct.POINTER(_ct.c_double))
        self._lib.itila_sc_feed_iq(self._h, ip, qp, _ct.c_int(n))

    def ready_bins(self):
        import ctypes as _ct
        buf = np.empty(self._max_bins, dtype=np.float64)
        ptr = buf.ctypes.data_as(_ct.POINTER(_ct.c_double))
        n = self._lib.itila_sc_ready_bins(self._h, ptr, _ct.c_int(self._max_bins))
        return set(buf[:n].tolist())

    def list_bins(self):
        import ctypes as _ct
        buf = np.empty(self._max_bins, dtype=np.float64)
        ptr = buf.ctypes.data_as(_ct.POINTER(_ct.c_double))
        n = self._lib.itila_sc_list_bins(self._h, ptr, _ct.c_int(self._max_bins))
        return set(buf[:n].tolist())

    def env_n(self, f_hz):
        import ctypes as _ct
        return self._lib.itila_sc_env_n(self._h, _ct.c_double(f_hz))

    def drain_env(self, f_hz, env100, env200, max_n):
        import ctypes as _ct
        p100 = env100.ctypes.data_as(_ct.POINTER(_ct.c_double))
        p200 = env200.ctypes.data_as(_ct.POINTER(_ct.c_double))
        return self._lib.itila_sc_drain_env(
            self._h, _ct.c_double(f_hz), p100, p200, _ct.c_int(max_n))

    def peek_env(self, f_hz, env100, env200, max_n):
        import ctypes as _ct
        p100 = env100.ctypes.data_as(_ct.POINTER(_ct.c_double))
        p200 = env200.ctypes.data_as(_ct.POINTER(_ct.c_double))
        return self._lib.itila_sc_peek_env(
            self._h, _ct.c_double(f_hz), p100, p200, _ct.c_int(max_n))

    def free(self):
        self._lib.itila_sc_free(self._h)
        self._h = None


def _get_itila_scanner(sample_rate, center_hz, max_bins, min_snr,
                        window_samples, energy_win, grid_hz,
                        band_min_hz, band_max_hz,
                        sos100, sos200):
    import ctypes as _ct
    try:
        lib = _ct.CDLL('./libitila_scanner.so')
        lib.itila_sc_create.restype  = _ct.c_void_p
        lib.itila_sc_create.argtypes = [
            _ct.c_int, _ct.c_double, _ct.c_int, _ct.c_double,
            _ct.c_int, _ct.c_int, _ct.c_double,
            _ct.c_double, _ct.c_double,
            _ct.POINTER(_ct.c_double), _ct.c_int,
            _ct.POINTER(_ct.c_double)]
        lib.itila_sc_free.restype    = None
        lib.itila_sc_free.argtypes   = [_ct.c_void_p]
        lib.itila_sc_feed_iq.restype  = None
        lib.itila_sc_feed_iq.argtypes = [
            _ct.c_void_p,
            _ct.POINTER(_ct.c_double), _ct.POINTER(_ct.c_double), _ct.c_int]
        lib.itila_sc_ready_bins.restype  = _ct.c_int
        lib.itila_sc_ready_bins.argtypes = [
            _ct.c_void_p, _ct.POINTER(_ct.c_double), _ct.c_int]
        lib.itila_sc_list_bins.restype  = _ct.c_int
        lib.itila_sc_list_bins.argtypes = [
            _ct.c_void_p, _ct.POINTER(_ct.c_double), _ct.c_int]
        lib.itila_sc_drain_env.restype  = _ct.c_int
        lib.itila_sc_drain_env.argtypes = [
            _ct.c_void_p, _ct.c_double,
            _ct.POINTER(_ct.c_double), _ct.POINTER(_ct.c_double), _ct.c_int]
        lib.itila_sc_bin_count.restype  = _ct.c_int
        lib.itila_sc_bin_count.argtypes = [_ct.c_void_p]
        lib.itila_sc_env_n.restype  = _ct.c_int
        lib.itila_sc_env_n.argtypes = [_ct.c_void_p, _ct.c_double]
        lib.itila_sc_mark_evidence.restype  = None
        lib.itila_sc_mark_evidence.argtypes = [_ct.c_void_p, _ct.c_double]
        lib.itila_sc_get_snr.restype  = _ct.c_double
        lib.itila_sc_get_snr.argtypes = [_ct.c_void_p, _ct.c_double]
        lib.itila_sc_peek_env.restype  = _ct.c_int
        lib.itila_sc_peek_env.argtypes = [
            _ct.c_void_p, _ct.c_double,
            _ct.POINTER(_ct.c_double), _ct.POINTER(_ct.c_double), _ct.c_int]
        lib.itila_sc_set_decoder.restype  = None
        lib.itila_sc_set_decoder.argtypes = [
            _ct.c_void_p,
            _ct.c_void_p, _ct.c_void_p, _ct.c_void_p, _ct.c_void_p,
            _ct.c_double]
        lib.itila_sc_decode_ready.restype  = _ct.c_int
        lib.itila_sc_decode_ready.argtypes = [
            _ct.c_void_p, _ct.c_int,
            _ct.c_void_p, _ct.c_int]

        n_sos = sos100.shape[0]
        s100  = np.ascontiguousarray(sos100, dtype=np.float64)
        s200  = np.ascontiguousarray(sos200, dtype=np.float64)
        p100  = s100.ctypes.data_as(_ct.POINTER(_ct.c_double))
        p200  = s200.ctypes.data_as(_ct.POINTER(_ct.c_double))
        h = lib.itila_sc_create(
            _ct.c_int(sample_rate), _ct.c_double(center_hz),
            _ct.c_int(max_bins),    _ct.c_double(min_snr),
            _ct.c_int(window_samples), _ct.c_int(energy_win),
            _ct.c_double(grid_hz),
            _ct.c_double(band_min_hz), _ct.c_double(band_max_hz),
            p100, _ct.c_int(n_sos), p200)
        if not h:
            log.warning("itila_sc_create returned NULL")
            return None
        log.info("Loaded libitila_scanner.so (sr=%d center=%.0f Hz bins=%d)",
                 sample_rate, center_hz, max_bins)
        return _ItilaSc(lib, _ct.c_void_p(h))
    except OSError:
        log.warning("libitila_scanner.so not found")
        return None


# ---------------------------------------------------------------------------
# PFB-backed scanner — alternative to libitila_scanner.so. Same Python wrapper
# surface; replaces per-bin NCO+FIR with a shared polyphase channelizer.
# Selected by config flag use_pfb_scanner.
# ---------------------------------------------------------------------------
PFB_NCHAN      = 4096   # must match PSC_PFB_NCHAN in pfb_scanner.c
PFB_OVERSAMPLE = 2      # must match PSC_PFB_OVERSAMPLE in pfb_scanner.c

def _pfb_output_rate(sample_rate):
    """Envelope-sample rate produced by the PFB scanner. C-side uses the
    same int math, so this stays consistent."""
    return (sample_rate * PFB_OVERSAMPLE) // PFB_NCHAN


class _PFBLibShim:
    """Thin namespace that exposes pfb_sc_* symbols under itila_sc_* names.

    Lets _ItilaSc and _ItilaScanner reach into the PFB backend without any
    code changes: every call site that does ``self._sc._lib.itila_sc_xxx``
    transparently dispatches to ``pfb_sc_xxx`` on libpfb_scanner.so."""
    _aliases = {
        'itila_sc_feed_iq':       'pfb_sc_feed_iq',
        'itila_sc_ready_bins':    'pfb_sc_ready_bins',
        'itila_sc_list_bins':     'pfb_sc_list_bins',
        'itila_sc_drain_env':     'pfb_sc_drain_env',
        'itila_sc_peek_env':      'pfb_sc_peek_env',
        'itila_sc_env_n':         'pfb_sc_env_n',
        'itila_sc_bin_count':     'pfb_sc_bin_count',
        'itila_sc_mark_evidence': 'pfb_sc_mark_evidence',
        'itila_sc_get_snr':       'pfb_sc_get_snr',
        'itila_sc_set_decoder':   'pfb_sc_set_decoder',
        'itila_sc_decode_ready':  'pfb_sc_decode_ready',
        'itila_sc_env_drops':     'pfb_sc_env_drops',
        'itila_sc_bins_peak':     'pfb_sc_bins_peak',
        'itila_sc_free':          'pfb_sc_free',
    }

    def __init__(self, lib):
        self._real = lib

    def __getattr__(self, name):
        return getattr(self._real, self._aliases.get(name, name))


def _get_pfb_scanner(sample_rate, center_hz, max_bins, min_snr,
                     window_samples, energy_win, grid_hz,
                     band_min_hz, band_max_hz,
                     sos100, sos200):
    """Drop-in replacement for _get_itila_scanner that loads
    libpfb_scanner.so and returns an _ItilaSc-compatible wrapper.
    The PFB backend is selected when use_pfb_scanner=True."""
    import ctypes as _ct
    try:
        lib = _ct.CDLL('./libpfb_scanner.so')
        lib.pfb_sc_create.restype  = _ct.c_void_p
        lib.pfb_sc_create.argtypes = [
            _ct.c_int, _ct.c_double, _ct.c_int, _ct.c_double,
            _ct.c_int, _ct.c_int, _ct.c_double,
            _ct.c_double, _ct.c_double,
            _ct.POINTER(_ct.c_double), _ct.c_int,
            _ct.POINTER(_ct.c_double)]
        lib.pfb_sc_free.restype    = None
        lib.pfb_sc_free.argtypes   = [_ct.c_void_p]
        lib.pfb_sc_feed_iq.restype  = None
        lib.pfb_sc_feed_iq.argtypes = [
            _ct.c_void_p,
            _ct.POINTER(_ct.c_double), _ct.POINTER(_ct.c_double), _ct.c_int]
        lib.pfb_sc_ready_bins.restype  = _ct.c_int
        lib.pfb_sc_ready_bins.argtypes = [
            _ct.c_void_p, _ct.POINTER(_ct.c_double), _ct.c_int]
        lib.pfb_sc_list_bins.restype  = _ct.c_int
        lib.pfb_sc_list_bins.argtypes = [
            _ct.c_void_p, _ct.POINTER(_ct.c_double), _ct.c_int]
        lib.pfb_sc_drain_env.restype  = _ct.c_int
        lib.pfb_sc_drain_env.argtypes = [
            _ct.c_void_p, _ct.c_double,
            _ct.POINTER(_ct.c_double), _ct.POINTER(_ct.c_double), _ct.c_int]
        lib.pfb_sc_peek_env.restype  = _ct.c_int
        lib.pfb_sc_peek_env.argtypes = [
            _ct.c_void_p, _ct.c_double,
            _ct.POINTER(_ct.c_double), _ct.POINTER(_ct.c_double), _ct.c_int]
        lib.pfb_sc_bin_count.restype  = _ct.c_int
        lib.pfb_sc_bin_count.argtypes = [_ct.c_void_p]
        lib.pfb_sc_env_n.restype  = _ct.c_int
        lib.pfb_sc_env_n.argtypes = [_ct.c_void_p, _ct.c_double]
        lib.pfb_sc_mark_evidence.restype  = None
        lib.pfb_sc_mark_evidence.argtypes = [_ct.c_void_p, _ct.c_double]
        lib.pfb_sc_get_snr.restype  = _ct.c_double
        lib.pfb_sc_get_snr.argtypes = [_ct.c_void_p, _ct.c_double]
        lib.pfb_sc_env_drops.restype  = _ct.c_ulonglong
        lib.pfb_sc_env_drops.argtypes = [_ct.c_void_p]
        lib.pfb_sc_bins_peak.restype  = _ct.c_int
        lib.pfb_sc_bins_peak.argtypes = [_ct.c_void_p]
        lib.pfb_sc_set_decoder.restype  = None
        lib.pfb_sc_set_decoder.argtypes = [
            _ct.c_void_p,
            _ct.c_void_p, _ct.c_void_p, _ct.c_void_p, _ct.c_void_p,
            _ct.c_double]
        lib.pfb_sc_decode_ready.restype  = _ct.c_int
        lib.pfb_sc_decode_ready.argtypes = [
            _ct.c_void_p, _ct.c_int,
            _ct.c_void_p, _ct.c_int]
        lib.pfb_sc_n_chan.restype       = _ct.c_int
        lib.pfb_sc_n_chan.argtypes      = [_ct.c_void_p]
        lib.pfb_sc_bin_spacing.restype  = _ct.c_double
        lib.pfb_sc_bin_spacing.argtypes = [_ct.c_void_p]
        lib.pfb_sc_output_rate.restype  = _ct.c_int
        lib.pfb_sc_output_rate.argtypes = [_ct.c_void_p]

        n_sos = sos100.shape[0]
        s100 = np.ascontiguousarray(sos100, dtype=np.float64)
        s200 = np.ascontiguousarray(sos200, dtype=np.float64)
        p100 = s100.ctypes.data_as(_ct.POINTER(_ct.c_double))
        p200 = s200.ctypes.data_as(_ct.POINTER(_ct.c_double))
        h = lib.pfb_sc_create(
            _ct.c_int(sample_rate), _ct.c_double(center_hz),
            _ct.c_int(max_bins),    _ct.c_double(min_snr),
            _ct.c_int(window_samples), _ct.c_int(energy_win),
            _ct.c_double(grid_hz),
            _ct.c_double(band_min_hz), _ct.c_double(band_max_hz),
            p100, _ct.c_int(n_sos), p200)
        if not h:
            log.warning("pfb_sc_create returned NULL")
            return None
        h_void  = _ct.c_void_p(h)
        n_chan  = lib.pfb_sc_n_chan(h_void)
        out_rt  = lib.pfb_sc_output_rate(h_void)
        bsp     = lib.pfb_sc_bin_spacing(h_void)
        log.info("Loaded libpfb_scanner.so (sr=%d center=%.0f Hz "
                 "n_chan=%d bin_spacing=%.2f Hz output_rate=%d Hz max_bins=%d)",
                 sample_rate, center_hz, n_chan, bsp, out_rt, max_bins)
        sc = _ItilaSc(_PFBLibShim(lib), h_void)
        sc.envelope_rate = out_rt
        return sc
    except OSError:
        log.warning("libpfb_scanner.so not found")
        return None


class _ItilaChannel:
    """Streaming Bayesian CW decoder via libitila.so.

    Pipeline per 12kHz PCM block:
      12 kHz real PCM  →  mix at -pitch_hz  →  complex baseband
                       →  IIR Butterworth LPF (100 Hz + 200 Hz paths)
                       →  |z|  →  envelope at 12 kHz
                       →  60:1 block-avg  →  200 Hz envelope

    Accumulates window_sec of envelope, then calls itila_feed (dual-LPF,
    union callsigns).  Emits space-separated callsigns via read().
    """

    def __init__(self, rf_khz, ev_thresh=2.0, window_sec=120.0):
        import ctypes as _ct
        from scipy.signal import butter, sosfilt_zi

        self.rf_khz      = rf_khz
        self.decoded_text  = ''
        self.detected_wpm  = 0

        self._ev_thresh      = ev_thresh
        self._window_samples = int(window_sec * 200)  # samples at 200 Hz
        self._pending        = []

        # Phase-continuous mixing state
        self._phase = 0.0

        # IIR LPF at 12 kHz (applied independently to I and Q after mixing)
        fs_pcm = DECODER_RATE  # 12000 Hz
        self._sos_100 = butter(6, 100.0 / (fs_pcm / 2.0), btype='low', output='sos')
        self._sos_200 = butter(6, 200.0 / (fs_pcm / 2.0), btype='low', output='sos')
        zi_100 = sosfilt_zi(self._sos_100)
        zi_200 = sosfilt_zi(self._sos_200)
        self._zi_100_i = zi_100.copy()
        self._zi_100_q = zi_100.copy()
        self._zi_200_i = zi_200.copy()
        self._zi_200_q = zi_200.copy()

        # Decimation residuals (carry-over between PCM blocks)
        self._res_100 = np.zeros(0, dtype=np.float64)
        self._res_200 = np.zeros(0, dtype=np.float64)

        # Envelope accumulators at 200 Hz
        self._env_100 = []
        self._env_200 = []

        # libitila handles (one per LPF path)
        lib = _get_itila_lib()
        if lib:
            self._h100 = _ct.c_void_p(lib.itila_create(200, 100.0))
            self._h200 = _ct.c_void_p(lib.itila_create(200, 200.0))
        else:
            self._h100 = self._h200 = None
        self._lib = lib

    def feed_pcm(self, pcm_bytes, pitch_hz):
        """Feed 12kHz int16 PCM; mix CW tone at pitch_hz to DC, extract envelope."""
        from scipy.signal import sosfilt

        if not pcm_bytes:
            return

        pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
        n = len(pcm)
        if n == 0:
            return

        # Mix at -pitch_hz to shift CW tone to DC
        phase_inc = -2.0 * np.pi * pitch_hz / DECODER_RATE
        phases = self._phase + np.arange(n, dtype=np.float64) * phase_inc
        self._phase = (phases[-1] + phase_inc) % (2.0 * np.pi)
        mixed_i = pcm * np.cos(phases)
        mixed_q = pcm * np.sin(phases)

        # IIR LPF (100 Hz and 200 Hz paths) — streaming with zi state
        i_100, self._zi_100_i = sosfilt(self._sos_100, mixed_i, zi=self._zi_100_i)
        q_100, self._zi_100_q = sosfilt(self._sos_100, mixed_q, zi=self._zi_100_q)
        i_200, self._zi_200_i = sosfilt(self._sos_200, mixed_i, zi=self._zi_200_i)
        q_200, self._zi_200_q = sosfilt(self._sos_200, mixed_q, zi=self._zi_200_q)

        env_100_12k = np.sqrt(i_100**2 + q_100**2)
        env_200_12k = np.sqrt(i_200**2 + q_200**2)

        # 60:1 block-average decimate 12 kHz → 200 Hz
        for env_12k, res_attr, env_list in (
            (env_100_12k, '_res_100', self._env_100),
            (env_200_12k, '_res_200', self._env_200),
        ):
            env_all = np.concatenate([getattr(self, res_attr), env_12k])
            n_dec = (len(env_all) // 60) * 60
            setattr(self, res_attr, env_all[n_dec:])
            if n_dec > 0:
                env_list.extend(env_all[:n_dec].reshape(-1, 60).mean(axis=1).tolist())

        # Decode any complete windows
        while len(self._env_100) >= self._window_samples:
            self._decode_window()

    def _decode_window(self):
        import ctypes as _ct
        lib = self._lib
        if not lib:
            return

        env100 = np.array(self._env_100[:self._window_samples], dtype=np.float64)
        env200 = np.array(self._env_200[:self._window_samples], dtype=np.float64)
        self._env_100 = self._env_100[self._window_samples:]
        self._env_200 = self._env_200[self._window_samples:]

        seen = set()
        for h, env in ((self._h100, env100), (self._h200, env200)):
            if h is None or h.value is None:
                continue
            n = len(env)
            env_c = np.ascontiguousarray(env, dtype=np.float64)
            ptr = env_c.ctypes.data_as(_ct.POINTER(_ct.c_double))
            result = lib.itila_feed(h, ptr, _ct.c_int(n),
                                    _ct.c_double(self.rf_khz),
                                    _ct.c_double(self._ev_thresh))
            raw = result.decode('ascii', errors='replace').strip() if result else ''
            log.debug("ITILA window %.1f kHz: thresh=%.1f raw=%r", self.rf_khz, self._ev_thresh, raw[:80])
            if raw:
                wpm = int(round(lib.itila_get_wpm(h)))
                call = _itila_extract_cq_call(raw)
                if call and call not in seen:
                    seen.add(call)
                    self._pending.append(f'CQ {call} ')
                    if wpm > 0:
                        self.detected_wpm = wpm
                    log.info("ITILA %.1f kHz: %s %d WPM (from: %s)", self.rf_khz, call, wpm, raw[:60])

    def read(self):
        if not self._pending:
            return ''
        text = ''.join(self._pending)
        self._pending = []
        return text

    def kill(self):
        lib = self._lib
        if lib:
            if self._h100 and self._h100.value:
                lib.itila_free(self._h100)
                self._h100 = None
            if self._h200 and self._h200.value:
                lib.itila_free(self._h200)
                self._h200 = None


# ---------------------------------------------------------------------------
# _ItilaScanner — band-wide ITILA channelizer (FFT energy scan)
# ---------------------------------------------------------------------------

class _ItilaScanner:
    """Band-wide ITILA channelizer — thin Python wrapper over libitila_scanner.so.

    The full pipeline (FFT energy scan, bin spawn, mix+decimate+IIR+envelope,
    200 Hz accumulation) runs in C.  Python only calls itila_feed() on ready
    windows and routes spots.

    Pipeline per bin per block (C):
      192 kHz IQ → FFT scan → spawn → mix DC → 16:1 → IIR 100/200 Hz
                → |z| → 60:1 → 200 Hz accum → window ready
    Python:
      window ready → itila_feed() → callsign → collect()
    """

    def __init__(self, sample_rate, center_khz, ev_thresh=2.0,
                 window_sec=120.0, min_snr=12.0,
                 band_min_khz=0.0, band_max_khz=99999.0,
                 max_bins=80, use_pfb=False, valid_calls=None,
                 enable_caller_spotting=True):
        self.valid_calls = valid_calls or set()
        self.enable_caller_spotting = bool(enable_caller_spotting)
        from scipy.signal import butter
        self.ev_thresh       = ev_thresh
        self._window_sec     = window_sec
        self._use_pfb        = bool(use_pfb)

        # Envelope rate: 200 Hz for the per-bin FIR scanner; PFB output rate
        # otherwise (with current params: 192000*2/2048 = 187 Hz).  Window is
        # always window_sec long in seconds, just sized in actual samples.
        self._envelope_rate  = (_pfb_output_rate(sample_rate) if self._use_pfb
                                else 200)
        self._window_samples = int(window_sec * self._envelope_rate)

        fs_pcm = DECODER_RATE  # 12000 Hz
        sos_100 = butter(6, 100.0 / (fs_pcm / 2.0), btype='low', output='sos')
        sos_200 = butter(6, 200.0 / (fs_pcm / 2.0), btype='low', output='sos')

        # f_hz -> {h100, h200, pending} — itila decoder handles per bin
        self._bins = {}

        loader = _get_pfb_scanner if self._use_pfb else _get_itila_scanner
        # Per-bin scanner keeps its 50 Hz grid.  PFB scanner uses 50 Hz too;
        # the per-bin fine-tune NCO mix snaps the residual offset out, so
        # we don't lose resolution by spawning at sub-bin frequencies.
        grid_hz = 50.0
        self._sc = loader(
            sample_rate, center_khz * 1000.0, max_bins, min_snr,
            self._window_samples, 4096,
            grid_hz,
            band_min_khz * 1000.0, band_max_khz * 1000.0,
            sos_100.astype(np.float64), sos_200.astype(np.float64),
        )

        # Set up C decode loop — decoder handles managed by scanner
        import ctypes as _ct
        itila_lib = _get_itila_lib()
        if self._sc and itila_lib:
            self._sc._lib.itila_sc_set_decoder(
                self._sc._h,
                _ct.cast(itila_lib.itila_create, _ct.c_void_p),
                _ct.cast(itila_lib.itila_feed, _ct.c_void_p),
                _ct.cast(itila_lib.itila_free, _ct.c_void_p),
                _ct.cast(itila_lib.itila_get_wpm, _ct.c_void_p),
                _ct.c_double(ev_thresh))
            self._c_decode = True
            # Pre-allocate result buffer for C decode loop
            self._decode_results = (_ct.c_char * (280 * 128))()
        else:
            self._c_decode = False

    def _ensure_bin_handles(self, f_hz):
        """Create itila decoder handles for a C-spawned bin if not yet tracked."""
        if f_hz in self._bins:
            return
        import ctypes as _ct
        lib = _get_itila_lib()
        h100 = h200 = None
        if lib:
            rate = self._envelope_rate
            h100 = _ct.c_void_p(lib.itila_create(rate, 100.0))
            h200 = _ct.c_void_p(lib.itila_create(rate, 200.0))
        self._bins[f_hz] = {
            'h100': h100, 'h200': h200, 'pending': [], 'wpm': 0, 'snr': 0.0,
            'text_buf': '',       # rolling accumulated raw decode text
            'spotted': set(),     # calls already spotted on this bin
            'last_cq_time': 0.0,  # wall time of last CQ trigger on this bin
        }
        log.info("ITILA scanner: spawned %.1f kHz", f_hz / 1000.0)

    def feed_iq(self, i_arr, q_arr):
        if not self._sc:
            return
        i_c = np.ascontiguousarray(i_arr, dtype=np.float64)
        q_c = np.ascontiguousarray(q_arr, dtype=np.float64)
        self._sc.feed_iq(i_c, q_c)
        self._process_ready()

    def _process_ready(self):
        """Sync bin handles with C scanner and decode any ready windows."""
        if not self._sc:
            return
        active_hz = self._sc.list_bins()
        for f_hz in active_hz:
            self._ensure_bin_handles(f_hz)
        for f_hz in list(self._bins):
            if f_hz not in active_hz:
                self._free_bin_handles(f_hz)

        ready = self._sc.ready_bins()
        for f_hz in ready:
            st = self._bins.get(f_hz)
            if st is None:
                continue
            n_env = self._sc.env_n(f_hz)
            while n_env >= self._window_samples:
                self._decode_bin(f_hz, st)
                n_env = self._sc.env_n(f_hz)

    def _decode_bin(self, f_hz, st):
        import ctypes as _ct
        lib = _get_itila_lib()
        if not lib or not self._sc:
            return

        env100 = np.empty(self._window_samples, dtype=np.float64)
        env200 = np.empty(self._window_samples, dtype=np.float64)
        n_drained = self._sc.drain_env(f_hz, env100, env200, self._window_samples)
        if n_drained < self._window_samples:
            return

        f_khz = f_hz / 1000.0
        snr = self._sc._lib.itila_sc_get_snr(self._sc._h, _ct.c_double(f_hz))
        if snr > 0:
            st['snr'] = snr
        now = time.time()

        for h, env in ((st['h100'], env100), (st['h200'], env200)):
            if h is None or h.value is None:
                continue
            env_c = np.ascontiguousarray(env[:n_drained], dtype=np.float64)
            ptr = env_c.ctypes.data_as(_ct.POINTER(_ct.c_double))
            result = lib.itila_feed(h, ptr, _ct.c_int(n_drained),
                                    _ct.c_double(f_khz),
                                    _ct.c_double(self.ev_thresh))
            raw = result.decode('ascii', errors='replace').strip() if result else ''
            if not raw:
                continue
            log.info("ITILA raw %.1f kHz: %r", f_khz, raw[:80])
            wpm = int(round(lib.itila_get_wpm(h)))
            if wpm > 0:
                st['wpm'] = wpm

            # Accumulate raw text in rolling buffer (last 512 chars)
            st['text_buf'] = (st['text_buf'] + ' ' + raw)[-512:]

            # Check for CQ trigger in this window's raw text
            if CQ_PATTERNS.search(raw):
                st['last_cq_time'] = now

            # Path 1: direct extraction from this window (standard)
            call = _itila_extract_cq_call(raw, self.valid_calls)
            if call and call not in st['spotted']:
                st['spotted'].add(call)
                st['pending'].append(f'CQ {call} ')
                log.info("ITILA scan %.1f kHz: %s %d WPM (raw: %s)", f_khz, call, wpm, raw[:60])
                if self._sc:
                    self._sc._lib.itila_sc_mark_evidence(
                        self._sc._h, _ct.c_double(f_hz))

            # Path 2: context extraction — if CQ was seen on this bin
            # recently, extract callsigns from the accumulated buffer.
            # Two modes (controlled by enable_caller_spotting):
            #   true  → c042491 behavior: extract callers AND runner
            #           (74% → 91% recall vs CW Skimmer benchmark)
            #   false → runner-only via _itila_extract_cq_call (precision-
            #           focused; emits 1 spot per pile-up not N)
            elif now - st['last_cq_time'] < 120.0:
                if self.enable_caller_spotting:
                    calls_in_raw = _itila_extract_all_calls(st['text_buf'])
                    for c in calls_in_raw:
                        if c not in st['spotted']:
                            st['spotted'].add(c)
                            st['pending'].append(f'CQ {c} ')
                            log.info("ITILA context %.1f kHz: %s %d WPM (CQ %ds ago, all)",
                                     f_khz, c, wpm, int(now - st['last_cq_time']))
                            if self._sc:
                                self._sc._lib.itila_sc_mark_evidence(
                                    self._sc._h, _ct.c_double(f_hz))
                else:
                    call = _itila_extract_cq_call(st['text_buf'], self.valid_calls)
                    if call and call not in st['spotted']:
                        st['spotted'].add(call)
                        st['pending'].append(f'CQ {call} ')
                        log.info("ITILA context %.1f kHz: %s %d WPM (CQ %ds ago, runner)",
                                 f_khz, call, wpm, int(now - st['last_cq_time']))
                        if self._sc:
                            self._sc._lib.itila_sc_mark_evidence(
                                self._sc._h, _ct.c_double(f_hz))

    def _process_ready_c(self):
        """C decode loop — all bin iteration + decode happens in C."""
        import ctypes as _ct
        if not self._sc or not getattr(self, '_c_decode', False):
            return
        # ScDecodeResult: {double f_hz(8), double snr(8), int wpm(4), pad(4), char text[256]}
        # Total: 280 bytes per result (with padding)
        max_results = 128
        result_size = 8 + 8 + 4 + 4 + 256  # 280 bytes
        buf = _ct.create_string_buffer(result_size * max_results)
        n = self._sc._lib.itila_sc_decode_ready(
            self._sc._h, _ct.c_int(self._window_samples),
            buf, _ct.c_int(max_results))

        now = time.time()
        for i in range(n):
            offset = i * result_size
            f_hz = _ct.c_double.from_buffer(buf, offset).value
            snr = _ct.c_double.from_buffer(buf, offset + 8).value
            wpm = _ct.c_int.from_buffer(buf, offset + 16).value
            raw = buf[offset + 24:offset + result_size].split(b'\0')[0].decode('ascii', errors='replace')

            if not raw:
                continue
            f_khz = f_hz / 1000.0
            log.info("ITILA raw %.1f kHz: %r", f_khz, raw[:80])

            # Ensure Python bin state exists for ticker tape
            if f_hz not in self._bins:
                self._bins[f_hz] = {
                    'h100': None, 'h200': None, 'pending': [], 'wpm': 0, 'snr': 0.0,
                    'text_buf': '', 'spotted': set(), 'last_cq_time': 0.0,
                }
            st = self._bins[f_hz]
            st['snr'] = snr
            if wpm > 0:
                st['wpm'] = wpm

            # Ticker tape: accumulate text
            st['text_buf'] = (st['text_buf'] + ' ' + raw)[-512:]
            if CQ_PATTERNS.search(raw):
                st['last_cq_time'] = now

            # Path 1: direct CQ extraction
            call = _itila_extract_cq_call(raw, self.valid_calls)
            if call and call not in st['spotted']:
                st['spotted'].add(call)
                st['pending'].append(f'CQ {call} ')
                log.info("ITILA scan %.1f kHz: %s %d WPM (raw: %s)", f_khz, call, wpm, raw[:60])
                if self._sc:
                    self._sc._lib.itila_sc_mark_evidence(
                        self._sc._h, _ct.c_double(f_hz))
            # Path 2: context extraction — see Python path above for the
            # caller-vs-runner-only mode flag (enable_caller_spotting).
            elif now - st['last_cq_time'] < 120.0:
                if self.enable_caller_spotting:
                    calls_in_raw = _itila_extract_all_calls(st['text_buf'])
                    for c in calls_in_raw:
                        if c not in st['spotted']:
                            st['spotted'].add(c)
                            st['pending'].append(f'CQ {c} ')
                            log.info("ITILA context %.1f kHz: %s %d WPM (CQ %ds ago, all)",
                                     f_khz, c, wpm, int(now - st['last_cq_time']))
                            if self._sc:
                                self._sc._lib.itila_sc_mark_evidence(
                                    self._sc._h, _ct.c_double(f_hz))
                else:
                    call = _itila_extract_cq_call(st['text_buf'], self.valid_calls)
                    if call and call not in st['spotted']:
                        st['spotted'].add(call)
                        st['pending'].append(f'CQ {call} ')
                        log.info("ITILA context %.1f kHz: %s %d WPM (CQ %ds ago, runner)",
                                 f_khz, call, wpm, int(now - st['last_cq_time']))
                        if self._sc:
                            self._sc._lib.itila_sc_mark_evidence(
                                self._sc._h, _ct.c_double(f_hz))

    def collect(self):
        """Returns list of (rf_khz, snr, text, text, bin_id, 'itila', wpm)."""
        results = []
        for f_hz, st in self._bins.items():
            if st['pending']:
                text = ''.join(st['pending'])
                st['pending'] = []
                results.append((f_hz/1000.0, st['snr'], text, text, id(st), 'itila', st['wpm']))
        return results

    def _free_bin_handles(self, f_hz):
        st = self._bins.pop(f_hz, None)
        if not st:
            return
        lib = _get_itila_lib()
        if lib:
            for h in (st['h100'], st['h200']):
                if h and h.value:
                    lib.itila_free(h)

    def kill(self):
        for f_hz in list(self._bins.keys()):
            self._free_bin_handles(f_hz)
        if self._sc:
            self._sc.free()
            self._sc = None


# ---------------------------------------------------------------------------
# libcw_dispatcher.so — batched parallel uhsdr fan-out
# ---------------------------------------------------------------------------
# One shared pool is created lazily the first time a _DispDecoder is spawned.
# Each _DispDecoder registers a channel in the pool and stashes its feed PCM
# into a per-instance buffer. Once per IQ block, InstanceManager.feed_all_iq
# calls _dispatcher_flush() which builds a contiguous (N, n_samples) int16
# batch and issues a single cw_disp_feed_batch call — the feed runs across
# all N channels in C++ with OpenMP, releasing the Python GIL for the whole
# fanout. Drained text is routed back into each _DispDecoder's pending buffer.

_cw_disp_lib = None
_cw_disp_handle = None        # opaque pool handle
_cw_disp_max_channels = 4096  # overridable via config
_dispatcher_instances = {}    # channel_id -> _DispDecoder (drain routing)


def _get_cw_dispatcher_lib():
    global _cw_disp_lib
    if _cw_disp_lib is not None:
        return _cw_disp_lib
    import ctypes as _ct
    try:
        _cw_disp_lib = _ct.CDLL('./libcw_dispatcher.so')
    except OSError:
        log.warning("libcw_dispatcher.so not found — dispatcher path disabled")
        _cw_disp_lib = False  # sentinel: tried and failed
        return None

    _cw_disp_lib.cw_disp_create.restype  = _ct.c_void_p
    _cw_disp_lib.cw_disp_create.argtypes = [_ct.c_int]
    _cw_disp_lib.cw_disp_destroy.restype  = None
    _cw_disp_lib.cw_disp_destroy.argtypes = [_ct.c_void_p]
    _cw_disp_lib.cw_disp_add_channel.restype  = _ct.c_int
    _cw_disp_lib.cw_disp_add_channel.argtypes = [
        _ct.c_void_p, _ct.c_float, _ct.c_float, _ct.c_int,
        _ct.c_float, _ct.c_float]
    _cw_disp_lib.cw_disp_remove_channel.restype  = None
    _cw_disp_lib.cw_disp_remove_channel.argtypes = [_ct.c_void_p, _ct.c_int]
    _cw_disp_lib.cw_disp_channel_count.restype  = _ct.c_int
    _cw_disp_lib.cw_disp_channel_count.argtypes = [_ct.c_void_p]
    _cw_disp_lib.cw_disp_feed_batch.restype  = _ct.c_int
    _cw_disp_lib.cw_disp_feed_batch.argtypes = [
        _ct.c_void_p,
        _ct.POINTER(_ct.c_int), _ct.c_int,
        _ct.POINTER(_ct.c_int16), _ct.c_int]
    _cw_disp_lib.cw_disp_drain.restype  = _ct.c_int
    _cw_disp_lib.cw_disp_drain.argtypes = [_ct.c_void_p, _ct.c_void_p, _ct.c_int]
    _cw_disp_lib.cw_disp_get_wpm.restype  = _ct.c_int
    _cw_disp_lib.cw_disp_get_wpm.argtypes = [_ct.c_void_p, _ct.c_int]

    # v2 PFB-fed entry points (added in cw_dispatcher v2). Optional — if the
    # library was built without them, these signature setups simply fail
    # silently and the v2 code path falls back.
    try:
        _cw_disp_lib.cw_disp_init_pfb.restype  = _ct.c_int
        _cw_disp_lib.cw_disp_init_pfb.argtypes = [
            _ct.c_void_p, _ct.c_int, _ct.c_int, _ct.c_int, _ct.c_int]
        _cw_disp_lib.cw_disp_add_pfb_channel.restype  = _ct.c_int
        _cw_disp_lib.cw_disp_add_pfb_channel.argtypes = [
            _ct.c_void_p, _ct.c_float, _ct.c_float, _ct.c_float,
            _ct.c_int, _ct.c_float, _ct.c_float]
        _cw_disp_lib.cw_disp_feed_iq.restype  = _ct.c_int
        _cw_disp_lib.cw_disp_feed_iq.argtypes = [
            _ct.c_void_p,
            _ct.POINTER(_ct.c_float), _ct.POINTER(_ct.c_float), _ct.c_int]
        _cw_disp_lib.cw_disp_get_channel_audio.restype  = _ct.c_int
        _cw_disp_lib.cw_disp_get_channel_audio.argtypes = [
            _ct.c_void_p, _ct.c_int, _ct.POINTER(_ct.c_int16), _ct.c_int]
    except AttributeError:
        log.warning("libcw_dispatcher.so missing v2 PFB symbols — v2 path disabled")

    # v3 bmorse entry points. Same fallback pattern.
    try:
        _cw_disp_lib.cw_disp_set_bmorse_fir.restype  = _ct.c_int
        _cw_disp_lib.cw_disp_set_bmorse_fir.argtypes = [
            _ct.c_void_p, _ct.POINTER(_ct.c_float), _ct.c_int]
        _cw_disp_lib.cw_disp_add_pfb_bmorse_channel.restype  = _ct.c_int
        _cw_disp_lib.cw_disp_add_pfb_bmorse_channel.argtypes = [
            _ct.c_void_p, _ct.c_float, _ct.c_float, _ct.c_float,
            _ct.c_int, _ct.c_float, _ct.c_float]
        _cw_disp_lib.cw_disp_set_bmorse_fir_narrow.restype  = _ct.c_int
        _cw_disp_lib.cw_disp_set_bmorse_fir_narrow.argtypes = [
            _ct.c_void_p, _ct.POINTER(_ct.c_float), _ct.c_int, _ct.c_int]
    except AttributeError:
        log.warning("libcw_dispatcher.so missing v3 bmorse symbols — bmorse-in-dispatcher disabled")

    log.info("Loaded libcw_dispatcher.so (parallel uhsdr fan-out)")
    return _cw_disp_lib


class _CWDecodedRecord:
    """Mirror of struct cw_decoded_record_t in cw_dispatcher.h."""
    # Declared lazily so ctypes isn't needed at module load.
    _ct_struct = None

    @classmethod
    def struct(cls):
        if cls._ct_struct is None:
            import ctypes as _ct
            class S(_ct.Structure):
                _fields_ = [
                    ('channel_id', _ct.c_int),
                    ('rf_khz',     _ct.c_float),
                    ('snr_db',     _ct.c_float),
                    ('wpm',        _ct.c_int),
                    ('text_len',   _ct.c_int),
                    ('text',       _ct.c_char * 256),
                ]
            cls._ct_struct = S
        return cls._ct_struct


def _get_cw_dispatcher_handle():
    """Return the process-wide dispatcher handle, creating it on first use."""
    global _cw_disp_handle
    if _cw_disp_handle is not None:
        return _cw_disp_handle
    lib = _get_cw_dispatcher_lib()
    if not lib:
        return None
    _cw_disp_handle = lib.cw_disp_create(_cw_disp_max_channels)
    if not _cw_disp_handle:
        log.error("cw_disp_create failed for max_channels=%d", _cw_disp_max_channels)
        return None
    log.info("Created cw_dispatcher pool (max_channels=%d)", _cw_disp_max_channels)
    return _cw_disp_handle


def _set_dispatcher_max_channels(n):
    """Called by InstanceManager before first decoder is spawned."""
    global _cw_disp_max_channels
    _cw_disp_max_channels = max(16, int(n))


class _DispDecoder:
    """Thin _LibDecoder-shaped shim that routes through libcw_dispatcher.so.

    feed_pcm() only stashes bytes into a per-instance buffer; the actual
    uhsdr_feed call happens in batch from _dispatcher_flush() once per IQ
    block. read() returns any text that drain has already routed here.
    """

    def __init__(self, rf_khz, snr, freq, sample_rate=12000, wpm=0):
        self.rf_khz = rf_khz
        self.snr = snr
        self.decoded_text = ''
        self.total_chars = 0
        self.last_output = time.time()
        self.detected_wpm = 0
        self._pending = ''
        self._stash = bytearray()

        lib = _get_cw_dispatcher_lib()
        h = _get_cw_dispatcher_handle() if lib else None
        if not h:
            self._cid = -1
            return
        self._lib = lib
        self._cid = lib.cw_disp_add_channel(
            h, float(freq), float(sample_rate), int(wpm),
            float(rf_khz), float(snr))
        if self._cid < 0:
            log.warning("cw_disp_add_channel failed — pool full? falling back for this instance")
            return
        _dispatcher_instances[self._cid] = self

    def feed_pcm(self, pcm_bytes):
        # Stash only; real feed happens in _dispatcher_flush().
        if self._cid < 0 or not pcm_bytes:
            return
        self._stash += pcm_bytes

    def read(self):
        t = self._pending
        self._pending = ''
        return t

    def kill(self):
        if self._cid < 0:
            return
        _dispatcher_instances.pop(self._cid, None)
        try:
            self._lib.cw_disp_remove_channel(_get_cw_dispatcher_handle(), self._cid)
        except Exception:
            pass
        self._cid = -1


def _dispatcher_flush():
    """Drain all _DispDecoder stash buffers into one batched feed + drain.

    Called from InstanceManager.feed_all_iq() once per IQ block when the
    dispatcher is enabled. Keeps the C-side call cost O(1) per block
    regardless of how many channels are active.
    """
    if not _dispatcher_instances:
        return
    lib = _cw_disp_lib
    if not lib:
        return
    h = _cw_disp_handle
    if not h:
        return

    import ctypes as _ct

    # Gather non-empty stashes. Channels with no pcm this round are skipped.
    live = [d for d in _dispatcher_instances.values()
            if d._cid >= 0 and len(d._stash) > 0]
    if not live:
        return

    # All channels fed by the same SignalGroup loop receive exactly the
    # same pcm_12k length per IQ block. But different groups may be at
    # different pipeline stages (e.g. waiting for pitch detection), so
    # stash lengths can differ. Bucket by length and fire one batch per
    # bucket — still a huge reduction vs one C call per decoder.
    from collections import defaultdict
    buckets = defaultdict(list)
    for d in live:
        buckets[len(d._stash)].append(d)

    int16_p = _ct.POINTER(_ct.c_int16)
    Record = _CWDecodedRecord.struct()
    MAX_DRAIN = 4096
    records = (Record * MAX_DRAIN)()

    for stash_len, group_decs in buckets.items():
        n_ch = len(group_decs)
        n_samples = stash_len // 2  # bytes → int16
        if n_samples == 0:
            for d in group_decs: d._stash.clear()
            continue

        # Build contiguous (n_ch, n_samples) int16 buffer. frombuffer is
        # zero-copy on bytes; we stack rows into a fresh array.
        rows = np.empty((n_ch, n_samples), dtype=np.int16)
        ids  = (_ct.c_int * n_ch)()
        for i, d in enumerate(group_decs):
            rows[i] = np.frombuffer(bytes(d._stash), dtype=np.int16)
            ids[i]  = d._cid
            d._stash.clear()

        rc = lib.cw_disp_feed_batch(
            h, ids, n_ch,
            rows.ctypes.data_as(int16_p), n_samples)
        if rc != 0:
            log.warning("cw_disp_feed_batch rc=%d n_ch=%d n_samples=%d",
                        rc, n_ch, n_samples)

    # One drain per flush catches all text from every bucket.
    n = lib.cw_disp_drain(h, _ct.cast(records, _ct.c_void_p), MAX_DRAIN)
    now = time.time()
    for i in range(n):
        r = records[i]
        d = _dispatcher_instances.get(r.channel_id)
        if d is None:
            continue
        text = bytes(r.text[:r.text_len]).decode('latin-1', errors='replace')
        d._pending      += text
        d.decoded_text  += text
        d.total_chars   += r.text_len
        d.last_output    = now
        if r.wpm > 0:
            d.detected_wpm = r.wpm


def _make_uhsdr_decoder(rf_khz, snr, freq, sample_rate, wpm, use_dispatcher):
    """Factory used by SignalGroup. Returns _DispDecoder when the flag is
    set AND the dispatcher lib is available; falls back to _LibDecoder
    otherwise so the baseline path is always reachable."""
    if use_dispatcher and _get_cw_dispatcher_lib():
        dec = _DispDecoder(rf_khz, snr, freq=freq,
                           sample_rate=sample_rate, wpm=wpm)
        if dec._cid >= 0:
            return dec
        # Pool full or add failed — fall through to _LibDecoder
    return _LibDecoder(rf_khz, snr, freq=freq,
                       sample_rate=sample_rate, wpm=wpm)


# ---------------------------------------------------------------------------
# libcw_dispatcher v2 — PFB lives in the dispatcher (IQ-fed path)
# ---------------------------------------------------------------------------
# When InstanceManager.use_pfb_dispatcher is set, the same shared dispatcher
# pool is reused but PFB lives inside the .so. The flow per IQ block is:
#
#   InstanceManager.feed_all_iq:
#     1. (legacy bmorse leg only) self._pfb.process(i, q)   # Python PFBChannelizer
#     2. cw_disp_feed_iq(d, i, q, n)                         # one C call → fanout
#     3. for each group: group.feed_iq(...)                  # only ch_4k pcm + bmorse
#     4. _pfb_disp_drain()                                   # walk drain output back
#
# Each _PFBDispDecoder is one uhsdr instance registered with the dispatcher
# via cw_disp_add_pfb_channel. SignalGroup may spawn many of them per signal
# (one per fixed speed, plus secondary-pitch ones once pitch is detected).
# A SignalGroup also gets a _PFBDispChannel that ducks the existing
# PFBChannel interface enough for SignalGroup.feed_iq to keep working — it
# does pitch detection by polling cw_disp_get_channel_audio on whichever of
# its decoders' channel_ids was registered first.

_pfb_disp_initialised = False
_pfb_disp_instances   = {}  # channel_id -> _PFBDispDecoder

def _pfb_dispatcher_init(input_rate, n_chan, oversample, taps_per_chan):
    """Initialise the dispatcher's PFB. Idempotent — second calls with
    matching parameters are no-ops, mismatched calls re-init."""
    global _pfb_disp_initialised
    lib = _get_cw_dispatcher_lib()
    if not lib or not hasattr(lib, 'cw_disp_init_pfb'):
        return False
    h = _get_cw_dispatcher_handle()
    if not h:
        return False
    rc = lib.cw_disp_init_pfb(h, int(input_rate), int(n_chan),
                              int(oversample), int(taps_per_chan))
    if rc != 0:
        log.error("cw_disp_init_pfb failed (rc=%d)", rc)
        return False
    _pfb_disp_initialised = True
    log.info("Initialised dispatcher PFB: input=%d n_chan=%d os=%d K=%d",
             input_rate, n_chan, oversample, taps_per_chan)
    return True


class _PFBDispDecoder:
    """uhsdr decoder backed by a PFB-aware dispatcher channel.

    Registers via cw_disp_add_pfb_channel — the dispatcher computes bin
    index, residual, decimation, and runs the per-block fanout in C++.
    feed_pcm() is a no-op (the dispatcher feeds via cw_disp_feed_iq at the
    InstanceManager level). Decoded text is routed here from the central
    drain inside _pfb_dispatcher_drain().
    """

    def __init__(self, rf_khz, snr, freq_offset_hz, tone_freq,
                 sample_rate=12000, wpm=0):
        self.rf_khz       = rf_khz
        self.snr          = snr
        self.decoded_text = ''
        self.total_chars  = 0
        self.last_output  = time.time()
        self.detected_wpm = 0
        self._pending     = ''
        self._spawn_wpm   = wpm   # WPM this instance was created with (0=auto)

        lib = _get_cw_dispatcher_lib()
        h = _get_cw_dispatcher_handle() if lib else None
        if not lib or not h or not _pfb_disp_initialised:
            self._cid = -1
            return
        self._lib = lib
        self._cid = lib.cw_disp_add_pfb_channel(
            h, float(freq_offset_hz), float(sample_rate), float(tone_freq),
            int(wpm), float(rf_khz), float(snr))
        if self._cid < 0:
            log.warning("cw_disp_add_pfb_channel failed for %.1f kHz "
                        "(pool full?) — will fall back per-instance", rf_khz)
            return
        _pfb_disp_instances[self._cid] = self

    # _LibDecoder-shaped interface ------------------------------------------
    def feed_pcm(self, pcm_bytes):
        # No-op — dispatcher already fed via cw_disp_feed_iq.
        return

    def read(self):
        t = self._pending
        self._pending = ''
        return t

    def kill(self):
        if self._cid < 0:
            return
        _pfb_disp_instances.pop(self._cid, None)
        try:
            self._lib.cw_disp_remove_channel(_get_cw_dispatcher_handle(), self._cid)
        except Exception:
            pass
        self._cid = -1


class _PFBDispChannel:
    """Duck-type stand-in for PFBChannel on the v2 path.

    The dispatcher does the actual PFB + per-channel work in C++, so this
    class doesn't process IQ at all. It only:
      - exposes the freq_offset / output_rate / detected_pitch attributes
        SignalGroup expects
      - polls cw_disp_get_channel_audio on its assigned decoder during the
        first ~15 seconds of life to run the same FFT-based pitch detector
        the Python PFBChannel uses.

    SignalGroup must call set_pitch_source(channel_id) once the first
    decoder is registered, otherwise pitch detection is skipped (defaults
    to CW_TONE).
    """

    def __init__(self, freq_offset, output_rate=DECODER_RATE):
        self.freq_offset       = freq_offset
        self.output_rate       = output_rate
        self._pitch_detected   = False
        self._pitch            = CW_TONE
        self._secondary_pitches      = []
        self._new_secondary_pitches  = []
        self._known_sec_pitches      = set()
        self._pitch_buf        = np.zeros(0, dtype=np.int16)
        self._pitch_source_cid = -1
        self._lib              = _get_cw_dispatcher_lib()
        self._handle           = _get_cw_dispatcher_handle() if self._lib else None
        self._audio_chunk      = None  # ctypes int16 array, lazy-allocated

    @property
    def detected_pitch(self):
        return self._pitch

    @property
    def secondary_pitches(self):
        return self._secondary_pitches

    @property
    def new_secondary_pitches(self):
        p = self._new_secondary_pitches[:]
        self._new_secondary_pitches = []
        return p

    def set_pitch_source(self, channel_id):
        self._pitch_source_cid = int(channel_id)

    def process(self, i_samples, q_samples):
        """No-op for audio (the dispatcher already handled it). Pitch
        detection happens here by pulling audio from cw_disp_get_channel_audio.
        Returns None — SignalGroup tolerates None for the uhsdr leg."""
        if self._pitch_detected or self._pitch_source_cid < 0:
            return None
        if not self._lib or not self._handle:
            return None

        import ctypes as _ct
        if self._audio_chunk is None:
            self._audio_chunk = (_ct.c_int16 * 8192)()

        # Drain whatever the dispatcher has accumulated for our pitch source
        # since the last call. (cw_disp_get_channel_audio consumes what it
        # returns; that's fine — only pitch detection wants this audio.)
        for _ in range(8):  # cap loop to avoid pulling forever
            got = self._lib.cw_disp_get_channel_audio(
                self._handle, self._pitch_source_cid,
                self._audio_chunk, 8192)
            if got <= 0:
                break
            new = np.frombuffer(self._audio_chunk, dtype=np.int16)[:got]
            self._pitch_buf = np.concatenate([self._pitch_buf, new])
            if got < 8192:
                break

        needed = self.output_rate * 15
        if len(self._pitch_buf) < needed:
            return None

        # Same pitch detection as PFBChannel.process()
        n_det = self.output_rate * 2
        spectrum = np.abs(np.fft.rfft(
            self._pitch_buf[:n_det].astype(np.float64) * np.hanning(n_det)))
        freqs = np.fft.rfftfreq(n_det, 1.0 / self.output_rate)
        mask = (freqs >= 475) & (freqs <= 825)
        if np.any(mask):
            spec_m = spectrum[mask]
            freqs_m = freqs[mask]
            peak_idx = np.argmax(spec_m)
            peak_freq = freqs_m[peak_idx]
            peak_amp = spec_m[peak_idx]
            self._pitch = max(450, min(850, int(round(peak_freq))))
            if abs(self._pitch - CW_TONE) > 5:
                log.info("PFB[v2] auto pitch: %d Hz (expected %d Hz)",
                         self._pitch, CW_TONE)
            threshold = peak_amp * 0.15
            candidates = []
            for i in range(len(freqs_m)):
                if abs(freqs_m[i] - peak_freq) <= 25:
                    continue
                if spec_m[i] < threshold:
                    continue
                lo, hi = max(0, i - 5), min(len(spec_m), i + 6)
                if spec_m[i] == np.max(spec_m[lo:hi]):
                    candidates.append((spec_m[i], int(round(freqs_m[i]))))
            candidates.sort(reverse=True)
            merged = []
            for amp, freq in candidates:
                if not any(abs(freq - mf) < 25 for _, mf in merged):
                    merged.append((amp, freq))
            self._secondary_pitches = [
                max(450, min(850, f)) for _, f in merged[:2]
            ]
            self._known_sec_pitches = set(self._secondary_pitches)
            if self._secondary_pitches:
                self._new_secondary_pitches = list(self._secondary_pitches)
                log.info("PFB[v2] secondary pitches: %s Hz alongside primary %d Hz",
                         self._secondary_pitches, self._pitch)
        self._pitch_detected = True
        self._pitch_buf = np.zeros(0, dtype=np.int16)
        return None


def _pfb_dispatcher_drain():
    """Walk cw_disp_drain output and route decoded text into each
    _PFBDispDecoder / _PFBDispBmorseDecoder's pending buffer. Called once
    per InstanceManager iteration when the v2/v3 path is active."""
    if not _pfb_disp_instances:
        return
    lib = _cw_disp_lib
    if not lib:
        return
    h = _cw_disp_handle
    if not h:
        return

    import ctypes as _ct
    Record = _CWDecodedRecord.struct()
    MAX_DRAIN = 4096
    records = (Record * MAX_DRAIN)()
    n = lib.cw_disp_drain(h, _ct.cast(records, _ct.c_void_p), MAX_DRAIN)
    now = time.time()
    for i in range(n):
        r = records[i]
        d = _pfb_disp_instances.get(r.channel_id)
        if d is None:
            continue
        text = bytes(r.text[:r.text_len]).decode('latin-1', errors='replace')
        d._pending      += text
        d.decoded_text  += text
        d.total_chars   += r.text_len
        d.last_output    = now
        if r.wpm > 0:
            d.detected_wpm = r.wpm


# v3 bmorse-in-dispatcher state ------------------------------------------
_bmorse_fir_installed = False

def _pfb_dispatcher_install_bmorse_fir(sample_rate=BMORSE_RATE,
                                       fir_bw_hz=400,
                                       fir_bw_narrow_hz=200,
                                       dual_threshold_wpm=20,
                                       n_taps=256,
                                       tone=None):
    """Install bandpass FIR taps for bmorse channels. Dual filter width:
    wide (fir_bw_hz, default 400) for fast CW, narrow (fir_bw_narrow_hz,
    default 200) for slow CW/DX (≤dual_threshold_wpm). SDC-inspired."""
    global _bmorse_fir_installed
    if _bmorse_fir_installed:
        return True
    lib = _get_cw_dispatcher_lib()
    if not lib or not hasattr(lib, 'cw_disp_set_bmorse_fir'):
        return False
    h = _get_cw_dispatcher_handle()
    if not h:
        return False

    from scipy.signal import firwin
    import ctypes as _ct
    centre = tone if tone is not None else CW_TONE

    # Wide taps (fast CW, >threshold WPM)
    lo_w = max(50.0, centre - fir_bw_hz / 2.0)
    hi_w = min(sample_rate / 2.0 - 50.0, centre + fir_bw_hz / 2.0)
    taps_wide = firwin(n_taps, [lo_w, hi_w], fs=sample_rate,
                       pass_zero=False).astype(np.float32)
    rc = lib.cw_disp_set_bmorse_fir(
        h, taps_wide.ctypes.data_as(_ct.POINTER(_ct.c_float)),
        int(len(taps_wide)))
    if rc != 0:
        log.error("cw_disp_set_bmorse_fir (wide) failed rc=%d", rc)
        return False

    # Narrow taps (slow CW/DX, ≤threshold WPM) — better SNR on weak signals
    lo_n = max(50.0, centre - fir_bw_narrow_hz / 2.0)
    hi_n = min(sample_rate / 2.0 - 50.0, centre + fir_bw_narrow_hz / 2.0)
    taps_narrow = firwin(n_taps, [lo_n, hi_n], fs=sample_rate,
                         pass_zero=False).astype(np.float32)
    if hasattr(lib, 'cw_disp_set_bmorse_fir_narrow'):
        rc = lib.cw_disp_set_bmorse_fir_narrow(
            h, taps_narrow.ctypes.data_as(_ct.POINTER(_ct.c_float)),
            int(len(taps_narrow)), int(dual_threshold_wpm))
        if rc != 0:
            log.warning("cw_disp_set_bmorse_fir_narrow failed rc=%d — single-width mode", rc)
        else:
            log.info("Installed dual bmorse FIR: wide %.0f–%.0f Hz, narrow %.0f–%.0f Hz, "
                     "threshold %d WPM @ %.0f Hz",
                     lo_w, hi_w, lo_n, hi_n, dual_threshold_wpm, sample_rate)
    else:
        log.info("Installed bmorse FIR (single width): %d taps, %.0f–%.0f Hz @ %.0f Hz",
                 n_taps, lo_w, hi_w, sample_rate)

    _bmorse_fir_installed = True
    return True


class _PFBDispBmorseDecoder:
    """bmorse decoder backed by a PFB-aware dispatcher channel.

    Mirrors the _PFBDispDecoder shape so _LibBmorseDecoder can be swapped
    out behind the v3 flag. feed_pcm() is a no-op (the dispatcher feeds
    via cw_disp_feed_iq at the InstanceManager level). Decoded text
    arrives via the shared _pfb_dispatcher_drain().
    """

    def __init__(self, rf_khz, snr, freq_offset_hz, tone_freq,
                 sample_rate=BMORSE_RATE, wpm=25):
        self.rf_khz       = rf_khz
        self.snr          = snr
        self.decoded_text = ''
        self.total_chars  = 0
        self.last_output  = time.time()
        self.detected_wpm = 0
        self._pending     = ''

        lib = _get_cw_dispatcher_lib()
        h = _get_cw_dispatcher_handle() if lib else None
        if (not lib or not h or not _pfb_disp_initialised
                or not hasattr(lib, 'cw_disp_add_pfb_bmorse_channel')):
            self._cid = -1
            return
        # Install the FIR lazily on first bmorse channel.
        if not _bmorse_fir_installed:
            if not _pfb_dispatcher_install_bmorse_fir(
                    sample_rate=sample_rate, tone=tone_freq):
                self._cid = -1
                return
        self._lib = lib
        self._cid = lib.cw_disp_add_pfb_bmorse_channel(
            h, float(freq_offset_hz), float(sample_rate), float(tone_freq),
            int(wpm), float(rf_khz), float(snr))
        if self._cid < 0:
            log.warning("cw_disp_add_pfb_bmorse_channel failed for %.1f kHz",
                        rf_khz)
            return
        _pfb_disp_instances[self._cid] = self

    def feed_pcm(self, pcm_bytes):
        return

    def read(self):
        t = self._pending
        self._pending = ''
        return t

    def kill(self):
        if self._cid < 0:
            return
        _pfb_disp_instances.pop(self._cid, None)
        try:
            self._lib.cw_disp_remove_channel(_get_cw_dispatcher_handle(), self._cid)
        except Exception:
            pass
        self._cid = -1


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
                 wpm=30, ml_model_path=None, ml_min_confidence=0.7,
                 pfb=None, use_dispatcher=False, use_pfb_dispatcher=False,
                 use_itila=False, center_khz=0, itila_ev_thresh=2.0,
                 itila_window_sec=120.0):
        self.freq_offset = freq_offset
        self.rf_khz = rf_khz
        self.snr = snr
        self.last_seen = time.time()
        self.last_output = time.time()
        self._use_dispatcher = use_dispatcher
        self._use_pfb_dispatcher = use_pfb_dispatcher
        self._last_cq_time = 0  # timestamp of last CQ/QRZ decoded (0 = never)

        # Try C++ cw_engine first (owns channelization + both decoders)
        self._cw_engine = None
        eng = _get_cw_engine() if speeds else None
        if eng:
            self._cw_engine = _CWEngineChannel(freq_offset, rf_khz, snr, sample_rate)

        # Python channelizers — use PFBChannel when PFB is available, else Channelizer.
        # PFBChannel extracts a 250 Hz-wide bin from the shared PFBChannelizer output,
        # naturally isolating co-channel signals that are ≥250 Hz apart.
        if pfb is not None:
            # On the v2 path the C++ dispatcher owns PFB + uhsdr fanout, so the
            # uhsdr leg is replaced with a no-op _PFBDispChannel that just runs
            # pitch detection. The bmorse leg (4 kHz) still uses the Python PFB.
            if use_pfb_dispatcher:
                self._ch_uhsdr = _PFBDispChannel(freq_offset, output_rate=DECODER_RATE)
            else:
                # No pre-filter — uhsdr's Goertzel IS the filter (47 Hz @ 700 Hz).
                # Each SignalGroup shifts its peak to CW_TONE; co-channel signals
                # land at offsets and the Goertzel rejects them naturally.
                # Adding a FIR here introduces group delay that breaks uhsdr timing.
                self._ch_uhsdr = PFBChannel(freq_offset, pfb, output_rate=DECODER_RATE,
                                            normalize='peak', cw_fir_bw=0)
            # 4 kHz leg: only needed for Python-side consumers (legacy bmorse,
            # hamfist, ML). In v3 (use_pfb_dispatcher=True), bmorse lives in
            # the dispatcher so it no longer needs ch_4k — we only spin it up
            # when ML is active for this group (top-N by SNR) or when hamfist
            # is wired. For the ~230 non-ML channels in a 250-cap eval this
            # skips a per-block numpy polyphase extract + FIR + normalise.
            if use_pfb_dispatcher:
                need_ch_4k = bool(ml_model_path) or bool(hamfist_bin)
            else:
                need_ch_4k = bool(bmorse_bin) or bool(hamfist_bin)
            self._ch_4k = PFBChannel(freq_offset, pfb, output_rate=BMORSE_RATE,
                                     normalize='peak',
                                     cw_fir_bw=400) if need_ch_4k else None
        else:
            self._ch_uhsdr = Channelizer(freq_offset, sample_rate, DECODER_RATE,
                                         normalize='peak', cw_fir_bw=1200)
            self._ch_4k = Channelizer(freq_offset, sample_rate, BMORSE_RATE,
                                      normalize='peak',
                                      cw_fir_bw=400) if (bmorse_bin or hamfist_bin) else None

        # Two-pass decoder spawn:
        #   Immediately: start uhsdr at default pitch (600 Hz) — no buffering
        #   After pitch detection (~15s) + uhsdr WPM (or 10s timeout): start bmorse
        self._decoder_bin = decoder_bin
        self._speeds = speeds
        self._pfb = pfb        # needed for lazy _ch_4k creation in bmorse fallback
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
                if self._use_pfb_dispatcher:
                    dec = _PFBDispDecoder(rf_khz, snr,
                                          freq_offset_hz=freq_offset,
                                          tone_freq=CW_TONE,
                                          sample_rate=DECODER_RATE,
                                          wpm=spd)
                    if dec._cid < 0:
                        # Fallback if pool full / not initialised
                        dec = _make_uhsdr_decoder(rf_khz, snr, freq=CW_TONE,
                                                  sample_rate=DECODER_RATE, wpm=spd,
                                                  use_dispatcher=self._use_dispatcher)
                elif lib:
                    dec = _make_uhsdr_decoder(rf_khz, snr, freq=CW_TONE,
                                              sample_rate=DECODER_RATE, wpm=spd,
                                              use_dispatcher=self._use_dispatcher)
                else:
                    cmd = [decoder_bin, '-r', str(DECODER_RATE), '-f', str(CW_TONE)]
                    if spd > 0:
                        cmd += ['-s', str(spd)]
                    dec = _SubprocessDecoder(rf_khz, snr, cmd, capture_wpm=True)
                dec._spawn_wpm = spd  # tag all paths uniformly (overrides _LibDecoder default)
                self.decoders.append(dec)
            # Tell the v2 _PFBDispChannel which channel to pull audio from
            # for pitch detection (the first successfully-registered decoder).
            if self._use_pfb_dispatcher and isinstance(self._ch_uhsdr, _PFBDispChannel):
                for dec in self.decoders:
                    cid = getattr(dec, '_cid', -1)
                    if cid >= 0:
                        self._ch_uhsdr.set_pitch_source(cid)
                        break

        self.bmorse = None
        self.hamfist = None
        self._secondary_decoders = []  # uhsdr instances for co-channel secondary pitches
        self._secondary_itila    = []  # _ItilaChannel instances for co-channel secondary pitches
        self._itila_ev_thresh    = itila_ev_thresh
        self._itila_window_sec   = itila_window_sec

        # WPM speed lock-in: after two consecutive scans with uhsdr_wpm > 0,
        # prune all but the best-matching speed instance to free channel slots.
        self._wpm_locked       = False
        self._wpm_stable_scans = 0
        self._ml_decoder = _MLDecoder(rf_khz, snr, ml_model_path, min_confidence=ml_min_confidence) if ml_model_path else None

        # ITILA Bayesian decoder (optional — accumulates envelope, batch decode)
        self._itila = (_ItilaChannel(rf_khz, ev_thresh=itila_ev_thresh,
                                     window_sec=itila_window_sec)
                       if use_itila else None)
        if self._itila:
            self._itila._pitch_hz = CW_TONE  # default; updated dynamically via feed_pcm

    @property
    def _decoders_started(self):
        """Backwards compat — uhsdr starts immediately now."""
        return True

    def _start_bmorse(self, wpm, pitch):
        """Start bmorse/hamfist with detected WPM and pitch.
        Also respawn uhsdr at the detected pitch if it differs from CW_TONE."""
        if not self._speeds and not self._bmorse_bin:
            self._bmorse_started = True
            return
        # Respawn uhsdr at detected pitch (was started at CW_TONE initially)
        if pitch != CW_TONE:
            for d in self.decoders:
                d.kill()
            self.decoders = []
            # Reset WPM lock — respawn restores all speed instances
            self._wpm_locked       = False
            self._wpm_stable_scans = 0
            lib = _get_uhsdr_lib()
            for spd in self._speeds:
                if self._use_pfb_dispatcher:
                    dec = _PFBDispDecoder(self.rf_khz, self.snr,
                                          freq_offset_hz=self.freq_offset,
                                          tone_freq=pitch,
                                          sample_rate=DECODER_RATE,
                                          wpm=spd)
                    if dec._cid < 0:
                        dec = _make_uhsdr_decoder(self.rf_khz, self.snr, freq=pitch,
                                                  sample_rate=DECODER_RATE, wpm=spd,
                                                  use_dispatcher=self._use_dispatcher)
                elif lib:
                    dec = _make_uhsdr_decoder(self.rf_khz, self.snr, freq=pitch,
                                              sample_rate=DECODER_RATE, wpm=spd,
                                              use_dispatcher=self._use_dispatcher)
                else:
                    cmd = [self._decoder_bin, '-r', str(DECODER_RATE), '-f', str(pitch)]
                    if spd > 0:
                        cmd += ['-s', str(spd)]
                    dec = _SubprocessDecoder(self.rf_khz, self.snr, cmd, capture_wpm=True)
                dec._spawn_wpm = spd
                self.decoders.append(dec)
            log.info("Respawned uhsdr at pitch=%d Hz for %.1f kHz", pitch, self.rf_khz)

        # Secondary pitch decoders — co-channel signals at different audio pitches
        # Uses the uhsdr channelizer (always available) to detect secondary peaks.
        # Only auto+detected-wpm (2 decoders per secondary pitch) to control CPU.
        sec_pitches = self._ch_uhsdr.secondary_pitches
        if sec_pitches:
            lib = _get_uhsdr_lib()
            sec_speeds = [0, wpm]  # auto + detected-wpm only (vs all 7 speeds)
            for sec_pitch in sec_pitches:
                for spd in sec_speeds:
                    if self._use_pfb_dispatcher:
                        dec = _PFBDispDecoder(self.rf_khz, self.snr,
                                              freq_offset_hz=self.freq_offset,
                                              tone_freq=sec_pitch,
                                              sample_rate=DECODER_RATE,
                                              wpm=spd)
                        if dec._cid < 0:
                            dec = _make_uhsdr_decoder(self.rf_khz, self.snr, freq=sec_pitch,
                                                      sample_rate=DECODER_RATE, wpm=spd,
                                                      use_dispatcher=self._use_dispatcher)
                    elif lib:
                        dec = _make_uhsdr_decoder(self.rf_khz, self.snr, freq=sec_pitch,
                                                  sample_rate=DECODER_RATE, wpm=spd,
                                                  use_dispatcher=self._use_dispatcher)
                    else:
                        cmd = [self._decoder_bin, '-r', str(DECODER_RATE),
                               '-f', str(sec_pitch)]
                        if spd > 0:
                            cmd += ['-s', str(spd)]
                        dec = _SubprocessDecoder(self.rf_khz, self.snr, cmd, capture_wpm=True)
                    self._secondary_decoders.append(dec)
            log.info("Spawned %d secondary-pitch uhsdr decoders at %s Hz for %.1f kHz",
                     len(self._secondary_decoders), sec_pitches, self.rf_khz)

        if self._bmorse_bin:
            # v3 dispatcher path — bmorse runs inside libcw_dispatcher.so,
            # fed by the shared PFB in C++. Falls back to _LibBmorseDecoder
            # if the dispatcher lib is missing the v3 symbols or the add
            # fails (pool full).
            self.bmorse = None
            if self._use_pfb_dispatcher:
                dec = _PFBDispBmorseDecoder(self.rf_khz, self.snr,
                                            freq_offset_hz=self.freq_offset,
                                            tone_freq=pitch,
                                            sample_rate=BMORSE_RATE,
                                            wpm=wpm)
                if dec._cid >= 0:
                    self.bmorse = dec
            if self.bmorse is None:
                bmlib = _get_bmorse_lib()
                if bmlib:
                    self.bmorse = _LibBmorseDecoder(self.rf_khz, self.snr, freq=pitch,
                                                    sample_rate=BMORSE_RATE, wpm=wpm)
                else:
                    log.warning("libbmorse.so not found — bmorse subprocess skipped"
                                " (binary requires WAV file, not piped PCM)")

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
                + len(self._secondary_decoders)
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

    def prune_to_locked_speed(self):
        """Drop all but the best-matching uhsdr speed decoder after WPM locks in.

        Called from InstanceManager every ~30 s (re-scan interval).  After
        uhsdr_wpm is stable (≥ 10 WPM) for two consecutive calls (≥ 60 s of
        audio), keep only the best-matching fixed-speed decoder + the auto
        (spawn_wpm=0) decoder, and kill the rest.  Retaining auto provides a
        safety net if the WPM lock-in was inaccurate.

        No-op if:
          - already locked (self._wpm_locked)
          - decoders already pruned to ≤ 1
          - uhsdr has not yet detected a WPM (returns 0)
          - detected WPM < 10 (noise read — reset stability counter)
        """
        if self._wpm_locked or len(self.decoders) <= 1:
            return
        wpm = self.uhsdr_wpm
        if wpm <= 0:
            self._wpm_stable_scans = 0
            return
        # Reject noise-induced low WPM readings — real CW is ≥ 10 WPM.
        if wpm < 10:
            self._wpm_stable_scans = 0
            return
        self._wpm_stable_scans += 1
        if self._wpm_stable_scans < 2:
            return  # wait for another scan to confirm stability

        # Find the decoder whose spawn WPM is closest to the locked speed.
        # Always keep the auto (spawn_wpm=0) decoder as a safety net alongside
        # the best fixed-speed match — protects against bad WPM lock-in.
        best      = None
        best_diff = 9999
        auto_dec  = None
        for d in self.decoders:
            spawn = getattr(d, '_spawn_wpm', -1)
            if spawn == 0:
                auto_dec = d   # always keep auto
                continue
            diff = abs(spawn - wpm)
            if diff < best_diff:
                best      = d
                best_diff = diff

        if best is None:
            # All decoders were auto — nothing to prune
            return

        # Keep best fixed-speed + auto; kill everything else.
        keep = {best}
        if auto_dec is not None:
            keep.add(auto_dec)
        victims = [d for d in self.decoders if d not in keep]
        for d in victims:
            d.kill()
        self.decoders      = [d for d in self.decoders if d in keep]
        self._wpm_locked   = True
        log.info("WPM lock %.1f kHz: %d WPM detected, pruned %d decoders → %d "
                 "(spawn_wpm=%d + auto)", self.rf_khz, wpm, len(victims),
                 len(self.decoders), getattr(best, '_spawn_wpm', -1))

    def feed_iq(self, i_samples, q_samples):
        # C++ cw_engine path: feed raw IQ (runs in parallel with Python)
        if self._cw_engine:
            self._cw_engine.feed_iq(i_samples, q_samples)

        # Python channelizer path (always runs)
        pcm_12k = self._ch_uhsdr.process(i_samples, q_samples)

        # ITILA path: tap Channelizer's 12kHz PCM; mix CW tone at detected_pitch to DC
        if pcm_12k:
            if self._itila:
                self._itila.feed_pcm(pcm_12k, self._ch_uhsdr.detected_pitch)
            # Spawn secondary ITILA channels when new pitches are detected — independent
            # of _start_bmorse so ITILA-only mode (decoder_speeds=[]) still gets them.
            if self._itila and self._ch_uhsdr.secondary_pitches:
                existing = {ch._pitch_hz for ch in self._secondary_itila}
                for sec_pitch in self._ch_uhsdr.secondary_pitches:
                    if sec_pitch not in existing:
                        ch = _ItilaChannel(self.rf_khz, ev_thresh=self._itila_ev_thresh,
                                           window_sec=self._itila_window_sec)
                        ch._pitch_hz = sec_pitch
                        self._secondary_itila.append(ch)
                        log.info("ITILA secondary channel spawned at %d Hz for %.1f kHz",
                                 sec_pitch, self.rf_khz)
            for ch in self._secondary_itila:
                ch.feed_pcm(pcm_12k, ch._pitch_hz)

        if self._ch_4k:
            pcm_4k = self._ch_4k.process(i_samples, q_samples)
        else:
            pcm_4k = None

        # uhsdr runs immediately — feed it always
        if pcm_12k:
            for d in self.decoders:
                d.feed_pcm(pcm_12k)
            for d in self._secondary_decoders:
                d.feed_pcm(pcm_12k)

        # Two-pass: bmorse waits for pitch detection (~15s) + uhsdr WPM (or timeout)
        if not self._bmorse_started:
            if pcm_4k:
                self._pcm_buffer_4k += pcm_4k
                # Feed ML decoder early — WPM estimate available in ~6s
                if self._ml_decoder:
                    self._ml_decoder.feed_pcm(pcm_4k)

            # Wait for pitch detection first
            pitch_ch = self._ch_4k if self._ch_4k else self._ch_uhsdr
            if not pitch_ch._pitch_detected:
                return

            # Pitch ready — start WPM timeout if not already set
            if self._bmorse_spawn_time == 0:
                self._bmorse_spawn_time = time.time() + 10

            pitch = pitch_ch.detected_pitch
            uhsdr_wpm = self.uhsdr_wpm
            ml_wpm = self._ml_decoder.detected_wpm if self._ml_decoder else 0
            if uhsdr_wpm > 0:
                self._start_bmorse(uhsdr_wpm, pitch)
            elif time.time() >= self._bmorse_spawn_time:
                best_wpm = ml_wpm if ml_wpm > 0 else self._wpm
                if ml_wpm > 0:
                    log.info("ML WPM pre-seed: %.1f kHz → %d WPM", self.rf_khz, ml_wpm)
                self._start_bmorse(best_wpm, pitch)
            return

        # Steady state: feed bmorse/hamfist (subprocess, if configured)
        if pcm_4k:
            if self.bmorse:
                self.bmorse.feed_pcm(pcm_4k)
            if self.hamfist:
                self.hamfist.feed_pcm(pcm_4k)

        # Second-pass bmorse fallback: spawn libbmorse when uhsdr hasn't decoded.
        # Skipped entirely on the v3 dispatcher path — dispatcher bmorse is
        # already running for this channel and a Python _LibBmorseDecoder
        # fallback would double-feed.
        if not self._bmorse_fallback_started and not self._use_pfb_dispatcher:
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
                if real_chars < 20 and self._bmorse_bin:  # uhsdr struggling; only if bmorse configured
                    bmlib = _get_bmorse_lib()
                    if bmlib:
                        pitch_ch = self._ch_4k if self._ch_4k else self._ch_uhsdr
                        pitch = pitch_ch.detected_pitch if pitch_ch._pitch_detected else CW_TONE
                        ml_wpm = self._ml_decoder.detected_wpm if self._ml_decoder else 0
                        wpm = self.uhsdr_wpm or ml_wpm or 25
                        self._bmorse_fallback = _LibBmorseDecoder(
                            self.rf_khz, self.snr, freq=pitch,
                            sample_rate=BMORSE_RATE, wpm=wpm)
                        # Create lazy 4kHz channelizer for bmorse fallback
                        if self._ch_4k is None:
                            if self._pfb is not None:
                                self._ch_4k = PFBChannel(
                                    self.freq_offset, self._pfb,
                                    output_rate=BMORSE_RATE, normalize='peak',
                                    cw_fir_bw=400)
                            else:
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
        """Returns list of (rf_khz, snr, new_text, accumulated_text, dec_id, dec_type, wpm)."""
        results = []
        if self._cw_engine:
            for di in range(self._cw_engine.decoder_count):
                text, wpm, speed = self._cw_engine.read_decoder_text(di)
                if text:
                    acc = self._cw_engine._accumulated
                    acc[di] = acc.get(di, '') + text
                    dec_type = 'primary'
                    dec_id = id(self._cw_engine) + di
                    results.append((self.rf_khz, self.snr, text, acc[di], dec_id, dec_type, wpm))
                    self.last_output = time.time()
        for d in self.decoders:
            text = d.read()
            if text:
                if not self._bmorse_started:
                    continue
                results.append((self.rf_khz, self.snr, text, d.decoded_text, id(d), 'primary', d.detected_wpm))
                self.last_output = time.time()
        for d in self._secondary_decoders:
            text = d.read()
            if text:
                if not self._bmorse_started:
                    continue
                results.append((self.rf_khz, self.snr, text, d.decoded_text, id(d), 'primary', d.detected_wpm))
                self.last_output = time.time()
        for d in ([self.bmorse] if self.bmorse else []) + \
                 ([self.hamfist] if self.hamfist else []) + \
                 ([self._bmorse_fallback] if self._bmorse_fallback else []):
            text = d.read()
            if text:
                results.append((self.rf_khz, self.snr, text, d.decoded_text, id(d), 'secondary', d.detected_wpm))
                self.last_output = time.time()
        # ITILA decoder: emits space-separated callsigns as 'primary' text
        for itila_ch in ([self._itila] if self._itila else []) + self._secondary_itila:
            text = itila_ch.read()
            if text:
                results.append((self.rf_khz, self.snr, text,
                                 text, id(itila_ch),
                                 'itila', itila_ch.detected_wpm))
                self.last_output = time.time()

        # Track CQ state: if any new text contains a CQ/QRZ pattern, mark time
        if results:
            all_new = ' '.join(r[2] for r in results)
            if CQ_PATTERNS.search(all_new):
                self._last_cq_time = time.time()
        return results

    def kill(self):
        if self._cw_engine:
            self._cw_engine.kill()
            self._cw_engine = None
        if self._itila:
            self._itila.kill()
            self._itila = None
        for ch in self._secondary_itila:
            ch.kill()
        self._secondary_itila = []
        for d in self.decoders:
            d.kill()
        for d in self._secondary_decoders:
            d.kill()
        self._secondary_decoders = []
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
                 max_instances=150, max_channels=None, signal_timeout=90,
                 speeds=None, bmorse_bin=None, hamfist_bin=None,
                 hamfist_scp=None, ml_model_path=None, ml_min_confidence=0.7,
                 ml_max_channels=20, use_dispatcher=False,
                 use_pfb_dispatcher=False, use_itila=False,
                 itila_ev_thresh=2.0, itila_window_sec=120.0,
                 itila_min_snr=8.0, itila_max_bins=200,
                 use_pfb_scanner=False, valid_calls=None,
                 cw_min_khz=0.0, cw_max_khz=99999.0,
                 enable_caller_spotting=True):
        self.valid_calls = valid_calls or set()
        self.sample_rate = sample_rate
        self.decoder_bin = decoder_bin
        self.bmorse_bin = bmorse_bin      # None = no bmorse
        self.hamfist_bin = hamfist_bin    # None = no HamFist
        self.hamfist_scp = hamfist_scp
        self.ml_model_path = ml_model_path  # None = no ML decoder
        self.ml_min_confidence = ml_min_confidence
        self.ml_max_channels = ml_max_channels  # cap ML to top-N SNR channels
        self.use_itila = bool(use_itila)
        self.itila_ev_thresh = float(itila_ev_thresh)
        self.itila_window_sec = float(itila_window_sec)
        self.itila_min_snr = float(itila_min_snr)
        self.itila_max_bins = int(itila_max_bins)
        self.use_pfb_scanner = bool(use_pfb_scanner)
        self.enable_caller_spotting = bool(enable_caller_spotting)
        self.cw_min_khz = float(cw_min_khz)
        self.cw_max_khz = float(cw_max_khz)
        self._itila_scanner = None  # created lazily in update_signals once center_khz is known
        self.max_instances = max_instances  # total decoder process cap (legacy)
        # max_channels: max simultaneous signals — decoupled from decoder count
        # defaults to max_instances for backwards compat
        self.max_channels = max_channels if max_channels is not None else max_instances
        self.signal_timeout = signal_timeout
        self.speeds = speeds if speeds is not None else [0, 30]  # auto + 30 WPM
        # freq_key -> SignalGroup
        self.instances = {}
        self.center_khz = 0
        # WPM cache: freq_key -> last known WPM (survives signal eviction)
        self._wpm_cache = {}
        # Pileup persistence tracker: centroid_bin -> {'centroid': kHz, 'passes': int, 'last': time}
        # centroid_bin = round(freq_khz * 5) / 5  (200 Hz grid)
        self._pileup_history = {}
        # Shared PFB channelizer — only needed by uhsdr/bmorse decoders.
        # Skip when decoder_speeds is empty and bmorse is unconfigured to
        # avoid ~200ms of wasted Python filtering per feed_all_iq call.
        _need_pfb = (bool(speeds) or bmorse_bin is not None) and sample_rate == 192000
        self._pfb = PFBChannelizer(input_rate=sample_rate) if _need_pfb else None
        # libcw_dispatcher.so fan-out (experimental). When enabled, uhsdr
        # decoders are routed via the C++ dispatcher so the per-IQ-block
        # fanout runs in OpenMP instead of a GIL-serialized Python loop.
        # use_pfb_dispatcher (v2) takes precedence over use_dispatcher (v1).
        self.use_pfb_dispatcher = bool(use_pfb_dispatcher)
        self.use_dispatcher = bool(use_dispatcher) and not self.use_pfb_dispatcher
        if self.use_dispatcher or self.use_pfb_dispatcher:
            # Pre-provision the pool so all add_channel calls can succeed.
            # At max_channels logical signals × up to ~9 uhsdr instances
            # per signal (5 fixed speeds + 2 secondary pitches × 2 speeds),
            # we want ~9× headroom. Round up.
            _set_dispatcher_max_channels(self.max_channels * 16)
            if _get_cw_dispatcher_lib() is None:
                log.warning("dispatcher enabled but libcw_dispatcher.so not loaded — falling back")
                self.use_dispatcher = False
                self.use_pfb_dispatcher = False
        if self.use_pfb_dispatcher and self._pfb is not None:
            # Init the C++ PFB to match the Python PFBChannelizer geometry
            # (so a SignalGroup at offset X computes the same bin in both).
            ok = _pfb_dispatcher_init(
                input_rate=self._pfb.input_rate,
                n_chan=self._pfb.N_CHAN,
                oversample=self._pfb.OVERSAMPLE,
                taps_per_chan=self._pfb.TAPS_PER_CHAN)
            if not ok:
                log.warning("cw_disp_init_pfb failed — disabling v2 path")
                self.use_pfb_dispatcher = False

    def update_signals(self, signals, center_khz, wpm_hint=0):
        """Update instance list based on detected signals.

        signals:  list of (offset_hz, snr_db) from FFT
        wpm_hint: ML-estimated WPM for this batch (0 = use default 30).
        """
        self.center_khz = center_khz
        now = time.time()

        # Lazily create or re-center the ITILA scanner when center_khz is known.
        if self.use_itila:
            if self._itila_scanner is None:
                # Derive per-band CW limits from center frequency if global limits
                # don't make sense for this band (multi-band operation)
                bmin, bmax = self.cw_min_khz, self.cw_max_khz
                if bmin == 0 or center_khz < bmin - 100 or center_khz > bmax + 100:
                    bmin = center_khz - 100
                    bmax = center_khz - 20
                self._itila_scanner = _ItilaScanner(
                    self.sample_rate, center_khz,
                    ev_thresh=self.itila_ev_thresh,
                    window_sec=self.itila_window_sec,
                    min_snr=float(self.itila_min_snr),
                    band_min_khz=bmin,
                    band_max_khz=bmax,
                    max_bins=int(self.itila_max_bins),
                    use_pfb=self.use_pfb_scanner,
                    valid_calls=getattr(self, 'valid_calls', None),
                    enable_caller_spotting=self.enable_caller_spotting)
            else:
                self._itila_scanner.center_khz = center_khz

        # In ITILA-only mode (no decoders) the scanner handles everything;
        # skip the SignalGroup machinery entirely.
        itila_only = (self.use_itila and not self.speeds and not self.bmorse_bin
                      and not self.hamfist_bin and not self.ml_model_path)
        if itila_only:
            return

        # Mark existing groups as seen if signal still present.
        # Use 150 Hz hysteresis: FFT peaks can wander ±50 Hz between rescans
        # (different noise floor, slightly different signal level). Without this,
        # a 51 Hz drift resets the SignalGroup, killing ITILA's 120s accumulation.
        matched_keys = set()
        for offset, snr in signals:
            best_key = None
            best_dist = 150  # Hz — hysteresis window; FFT peaks can wander ±50 Hz
            for k in self.instances:
                d = abs(offset - k)
                if d < best_dist:
                    best_dist = d
                    best_key = k
            if best_key is not None:
                self.instances[best_key].last_seen = now
                self.instances[best_key].snr = max(self.instances[best_key].snr, snr)
                matched_keys.add(best_key)

        spd = wpm_hint if wpm_hint > 0 else 25  # default 25 (contest CW center)

        # Spawn new SignalGroup for new signals
        for offset, snr in sorted(signals, key=lambda x: -x[1]):
            key = int(round(offset / 100)) * 100
            # Skip if already matched to an existing group within 150 Hz
            if any(abs(offset - k) < 150 for k in matched_keys):
                continue
            if key in self.instances:
                continue
            if abs(offset) < 100:  # skip DC
                continue
            if len(self.instances) >= self.max_channels:
                # Evict the lowest-SNR group if new signal is stronger.
                # WPM-aware: protect slow/weak DX (≤20 WPM) from being
                # bumped by fast contest stations (>20 WPM). SDC does
                # this with pile-up spatial classification; we use WPM
                # as a proxy since DX stations are typically slower.
                if not self.instances:
                    break
                weakest_key = min(self.instances, key=lambda k: self.instances[k].snr)
                weakest = self.instances[weakest_key]
                incumbent_wpm = weakest.uhsdr_wpm

                # Determine eviction threshold based on WPM
                if (incumbent_wpm > 0 and incumbent_wpm <= 20):
                    # Incumbent is slow (likely DX/CQ) — protect it.
                    # Require 10 dB margin to evict, not the default 3.
                    eviction_margin = 10
                else:
                    eviction_margin = 3  # default: challenger must be ≥3 dB stronger

                if snr <= weakest.snr + eviction_margin:
                    break  # new signal not strong enough to justify eviction
                evicted = self.instances.pop(weakest_key)
                # Cache WPM from evicted signal for future respawns
                evicted_wpm = evicted.uhsdr_wpm
                if evicted_wpm > 0:
                    self._wpm_cache[weakest_key] = evicted_wpm
                log.info("Evicted %.1f kHz (%+.0f dB, %d WPM) for %.1f kHz (%+.0f dB) [margin=%d]",
                         center_khz + weakest_key/1000, evicted.snr,
                         evicted_wpm if evicted_wpm else 0,
                         center_khz + offset/1000, snr, eviction_margin)
                evicted.kill()

            rf_khz = center_khz + offset / 1000
            # Use cached WPM if available, otherwise default
            cached_wpm = self._wpm_cache.get(key, 0)
            signal_wpm = cached_wpm if cached_wpm > 0 else spd
            # Gate ML to top-N channels by SNR (signals sorted strongest-first)
            ml_count = sum(1 for g in self.instances.values() if g._ml_decoder is not None)
            use_ml = self.ml_model_path if ml_count < self.ml_max_channels else None
            group = SignalGroup(
                offset, rf_khz, self.sample_rate, snr,
                decoder_bin=self.decoder_bin,
                speeds=self.speeds,
                bmorse_bin=self.bmorse_bin,
                hamfist_bin=self.hamfist_bin,
                hamfist_scp=self.hamfist_scp,
                wpm=signal_wpm,
                ml_model_path=use_ml,
                ml_min_confidence=self.ml_min_confidence,
                pfb=self._pfb,
                use_dispatcher=self.use_dispatcher,
                use_pfb_dispatcher=self.use_pfb_dispatcher,
                use_itila=self.use_itila,
                center_khz=self.center_khz,
                itila_ev_thresh=self.itila_ev_thresh,
                itila_window_sec=self.itila_window_sec,
            )
            self.instances[key] = group
            extras = ('+bmorse' if self.bmorse_bin else '') + \
                     ('+hamfist' if self.hamfist_bin else '') + \
                     ('+itila' if self.use_itila else '')
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

        # WPM speed lock-in: prune redundant uhsdr speed instances once WPM is stable.
        # Each surviving group that has locked WPM goes from len(speeds) decoders → 1,
        # freeing dispatcher channel slots for new signals.
        for group in self.instances.values():
            group.prune_to_locked_speed()

    def feed_all_iq(self, i_samples, q_samples):
        """Feed IQ to all SignalGroups — each runs its shared channelizer."""
        # v2 PFB-fed dispatcher: one C call runs PFB + per-channel fanout +
        # uhsdr_feed for every PFB-aware channel. We still need the Python
        # PFBChannelizer for the bmorse 4 kHz leg if any group uses it.
        if self.use_pfb_dispatcher:
            import ctypes as _ct
            i_arr = np.ascontiguousarray(np.asarray(i_samples, dtype=np.float32))
            q_arr = np.ascontiguousarray(np.asarray(q_samples, dtype=np.float32))
            n = int(i_arr.size)
            lib = _cw_disp_lib
            h = _cw_disp_handle
            if lib and h and n > 0:
                lib.cw_disp_feed_iq(
                    h,
                    i_arr.ctypes.data_as(_ct.POINTER(_ct.c_float)),
                    q_arr.ctypes.data_as(_ct.POINTER(_ct.c_float)),
                    n)

        # Run Python PFB once for all channels (if available). v2 still
        # needs this for the bmorse 4 kHz leg; v1 needs it for everything.
        if self._pfb is not None:
            self._pfb.process(i_samples, q_samples)
        for group in list(self.instances.values()):
            group.feed_iq(i_samples, q_samples)
        if self._itila_scanner is not None:
            self._itila_scanner.feed_iq(i_samples, q_samples)

        # If the C++ dispatcher is enabled, every _DispDecoder.feed_pcm call
        # above just stashed bytes. Now drive one batched feed + drain for
        # the whole IQ block (single C call, runs fanout in OpenMP).
        if self.use_dispatcher:
            _dispatcher_flush()
        if self.use_pfb_dispatcher:
            _pfb_dispatcher_drain()

    def collect_all(self):
        """Read decoded text from all groups.

        Returns list of (rf_khz, snr, new_text, accumulated_text, dec_id, dec_type).
        """
        results = []
        for group in list(self.instances.values()):
            results.extend(group.read())
        if self._itila_scanner is not None:
            results.extend(self._itila_scanner.collect())
        return results

    def collect_engine_spots(self):
        """Read pre-validated spots from C++ cw_engine channels."""
        spots = []
        for group in list(self.instances.values()):
            spots.extend(group.read_engine_spots())
        return spots

    def kill_all(self):
        # Terminate all subprocesses simultaneously, then wait in batch.
        # Sequential kill() with wait(timeout=2) per process can take minutes
        # with 800+ decoders. Batch terminate → short wait → SIGKILL stragglers.
        import subprocess as _sp
        procs = []
        for group in self.instances.values():
            for d in list(group.decoders) + list(group._secondary_decoders):
                if getattr(d, 'process', None):
                    try: d.process.stdin.close()
                    except: pass
                    try: d.process.terminate()
                    except: pass
                    procs.append(d.process)
                    d.process = None
            for attr in ('bmorse', 'hamfist', '_bmorse_fallback', '_cw_engine'):
                obj = getattr(group, attr, None)
                if obj and getattr(obj, 'process', None):
                    try: obj.process.stdin.close()
                    except: pass
                    try: obj.process.terminate()
                    except: pass
                    procs.append(obj.process)
                    obj.process = None
        # Wait up to 3s total for all to exit, then SIGKILL remainder
        import time as _t
        deadline = _t.time() + 3.0
        for p in procs:
            remaining = max(0.05, deadline - _t.time())
            try: p.wait(timeout=remaining)
            except:
                try: p.kill()
                except: pass
        self.instances.clear()
        if self._itila_scanner is not None:
            self._itila_scanner.kill()
            self._itila_scanner = None

    def detect_pileups(self, spotted_freqs=None, now=None):
        """Detect confirmed pileup clusters — corrected geometry.

        now: override timestamp for persistence checks. Pass the audio-file
             simulation time (t_now) in file mode so persistence isn't
             measured in wall-clock seconds (which run 3-4× faster than
             audio time when processing is slow). Pass None in live mode
             to use time.time().

        Pileup geometry (from operating experience):
          - DX station transmits BELOW the cluster, listens in the pileup zone
          - Callers pile up ABOVE the DX listen frequency, spreading further upward
            as the pileup grows
          - The cluster FLOOR (lowest active caller) is the most stable anchor;
            the top grows upward over time
          - Inferred DX TX frequency = cluster floor − DX_OFFSET (typically 1-3 kHz below)

        Filters applied in order:
          1. Size ≥ 3 active non-CQ channels within 1 kHz span
          1b. Digital mode exclusion: cluster floor not within ±2 kHz of a known
              FT8/FT4 frequency (160m–6m). These produce tight non-CQ clusters
              that pass all other filters.
          2. Floor persistence: cluster floor stays within ±500 Hz between passes
             (≤ 90 s gap).  Floor may drift upward — that's normal pileup growth.
          3. No existing spot within ±1.5 kHz of cluster floor
          4. Uniform cluster SNR: no member ≥ 20 dB below cluster max
             (rejects contest QSOs where one dominant station skews the spread)

        spotted_freqs: set of kHz values recently spotted by SpotTracker.
                       Pass None to skip filter 3.

        Returns list of confirmed pileup dicts:
            floor_khz   — lowest caller frequency (kHz); closest to DX listen freq
            top_khz     — highest caller frequency (kHz)
            size        — number of active channels in cluster
            snr_max     — peak SNR in cluster
            dx_tx_khz   — inferred DX TX frequency (floor − DX_OFFSET); spawn decoder here
            members     — list of member channel frequencies (kHz)
            passes      — number of detection passes this cluster has persisted
        """
        wall_now = time.time()
        if now is None:
            now = wall_now
        CQ_GRACE        = 120.0  # s after last CQ before channel counts as non-CQ
        ACTIVE_WIN      = 45.0   # s since last_seen to be considered active
        CLUSTER_HZ      = 1000.0 # total span: callers within 1 kHz = typical pileup width
        MIN_SIZE        = 3      # filter 1
        FLOOR_DRIFT     = 1.0    # kHz — filter 2: floor may shift ≤1 kHz between passes
        PERSIST_MAX_GAP = 90.0   # s — filter 2: max gap between passes
        MIN_PASSES      = 2      # filter 2: passes before confirmed
        SPOT_RADIUS     = 1.5    # kHz — filter 3: no spot within this radius of floor
        SNR_SPREAD      = 20.0   # dB — filter 4: max spread across cluster members
        DX_OFFSET       = 1.5    # kHz below cluster floor where DX is likely transmitting

        # --- Build candidate list: active, has output, not recently CQ ---
        # Use wall_now for last_seen / _last_cq_time — those are wall-clock timestamps
        candidates = []
        for group in self.instances.values():
            if wall_now - group.last_seen > ACTIVE_WIN:
                continue
            if group.total_chars == 0:
                continue
            if group._last_cq_time > 0 and wall_now - group._last_cq_time < CQ_GRACE:
                continue
            candidates.append(group)

        # Expire stale history entries regardless
        stale = [k for k, v in self._pileup_history.items()
                 if now - v['last'] > PERSIST_MAX_GAP * 2]
        for k in stale:
            del self._pileup_history[k]

        if len(candidates) < MIN_SIZE:
            return []

        candidates.sort(key=lambda g: g.rf_khz)

        # --- Sliding-window raw cluster detection ---
        raw_clusters = []
        i = 0
        while i < len(candidates):
            j = i
            while j < len(candidates) and \
                  (candidates[j].rf_khz - candidates[i].rf_khz) * 1000 <= CLUSTER_HZ:
                j += 1
            window = candidates[i:j]
            if len(window) >= MIN_SIZE:
                freqs = [g.rf_khz for g in window]
                snrs  = [g.snr    for g in window]
                raw_clusters.append({
                    'floor':   min(freqs),
                    'top':     max(freqs),
                    'size':    len(window),
                    'snr_max': max(snrs),
                    'snr_min': min(snrs),
                    'members': freqs,
                })
                i = j
            else:
                i += 1

        # Diagnostic: log every raw cluster before filtering
        log.debug("PILEUP_RAW: %d candidates, %d raw clusters",
                  len(candidates), len(raw_clusters))
        for cl in raw_clusters:
            log.debug("PILEUP_RAW: floor=%.1f top=%.1f size=%d snr=[%d..%d] members=%s",
                      cl['floor'], cl['top'], cl['size'],
                      cl['snr_min'], cl['snr_max'],
                      ','.join('%.1f' % f for f in cl['members']))

        # Digital mode exclusion zones — ±2 kHz around known FT8/FT4 frequencies.
        # These produce tight clusters of non-CQ signals that pass all other filters.
        DIGITAL_EXCL_KHZ = [
            1840.0,   # 160m FT8
            3573.0,   # 80m FT8
            7047.0,   # 40m FT4
            7074.0,   # 40m FT8
            10136.0,  # 30m FT8
            14074.0,  # 20m FT8
            14080.0,  # 20m FT4
            18100.0,  # 17m FT8
            21074.0,  # 15m FT8
            24915.0,  # 12m FT8
            28074.0,  # 10m FT8
            28180.0,  # 10m FT4
            50313.0,  # 6m FT8
        ]
        DIGITAL_EXCL_RADIUS = 2.0  # kHz

        confirmed = []
        for cl in raw_clusters:
            floor = cl['floor']

            # Filter 1b: reject clusters near known digital mode frequencies
            excl_hit = next((f for f in DIGITAL_EXCL_KHZ
                             if abs(floor - f) <= DIGITAL_EXCL_RADIUS), None)
            if excl_hit is not None:
                log.debug("PILEUP_DBG: floor=%.1f excluded — within %.1f kHz of digital freq %.1f",
                          floor, abs(floor - excl_hit), excl_hit)
                continue

            # Filter 2: floor persistence — find matching history entry by floor proximity
            matched_key = None
            for k, entry in self._pileup_history.items():
                if abs(entry['floor'] - floor) <= FLOOR_DRIFT:
                    matched_key = k
                    break
            if matched_key is not None:
                entry = self._pileup_history[matched_key]
                if now - entry['last'] <= PERSIST_MAX_GAP:
                    entry['passes'] += 1
                    entry['last']    = now
                    entry['floor']   = floor  # update to latest observed floor
                else:
                    entry['passes']  = 1
                    entry['last']    = now
                    entry['floor']   = floor
            else:
                matched_key = id(cl)
                self._pileup_history[matched_key] = {
                    'floor': floor, 'passes': 1, 'last': now
                }

            passes = self._pileup_history[matched_key]['passes']
            if passes < MIN_PASSES:
                log.debug("PILEUP_DBG: floor=%.1f passes=%d < %d (need more)",
                          floor, passes, MIN_PASSES)
                continue

            # Filter 3: no existing spot near the cluster floor
            if spotted_freqs:
                blocking = [sf for sf in spotted_freqs if abs(sf - floor) <= SPOT_RADIUS]
                if blocking:
                    log.debug("PILEUP_DBG: floor=%.1f blocked by spot at %.1f",
                              floor, blocking[0])
                    continue

            # Filter 4: uniform cluster SNR
            spread = cl['snr_max'] - cl['snr_min']
            if spread > SNR_SPREAD:
                log.debug("PILEUP_DBG: floor=%.1f SNR spread=%.0f > %.0f (max=%d min=%d)",
                          floor, spread, SNR_SPREAD, cl['snr_max'], cl['snr_min'])
                continue

            dx_tx = floor - DX_OFFSET
            confirmed.append({
                'floor_khz':  floor,
                'top_khz':    cl['top'],
                'size':       cl['size'],
                'snr_max':    cl['snr_max'],
                'dx_tx_khz':  dx_tx,
                'members':    cl['members'],
                'passes':     self._pileup_history[matched_key]['passes'],
            })

        return confirmed

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

    # "Ship-it" defaults: all gates off, caller-spotting on, telemetry on.
    # Per the 2026-04-27 plan (feedback_ship_it_plan.md), local gates were
    # over-tuning precision and crashing recall. Default to permissive emit
    # and let the cluster filter (VE7CC's 2+ skimmer rule). Each gate
    # remains as a feature that can be re-enabled individually via config
    # for users who want stricter local behavior.
    GATE_DEFAULTS = {
        'gate_cq_runner':              False,  # CQ-adjacency forces runner-only when CQ in buffer
        'gate_freq_consensus':         False,  # per-freq commit + 5-min lock + adaptive thresh
        'gate_patt3ch_filter':         False,  # drop bypass calls not matching patt3ch.lst
        'gate_bypass_consensus':       False,  # bypass goes through freq consensus vs simple count
        'gate_scp_bucket_substitute':  False,  # emit bucket form instead of raw call
        'gate_recent_band_floor':      False,  # anchor solo decode if peers saw it recently (S-floor)
        'gate_harmonic_filter':        False,  # drop 2x-5x harmonic spurs of same-call recent spots
        'enable_caller_spotting':      True,   # extract callers AND runner from QSO buffer (c042491)
        'gate_telemetry':              True,   # log "would-gate" decisions even when gate is off
    }

    def __init__(self, valid_calls, blacklist, respot_interval=120,
                 fuzzy_min_cycles=3, add_calls=None, scp_bypass_threshold=0,
                 patt3ch_path='patt3ch.lst', gate_config=None,
                 recent_band_config=None):
        self.valid_calls = valid_calls
        self.blacklist = blacklist
        self.respot_interval = respot_interval
        self.fuzzy_min_cycles = fuzzy_min_cycles
        self.add_calls = add_calls or set()
        # Gate config — start with defaults, override anything in gate_config arg
        self.gate_config = dict(self.GATE_DEFAULTS)
        if gate_config:
            self.gate_config.update(gate_config)
        # Recent-on-band support floor (S-floor, ported from N2WQ's
        # GoCluster). When peer DX clusters spot a call on a band, we
        # remember it for `window_sec`. If our own decoder later sees
        # the same call on the same band — even on a single sighting
        # — we trust it because peer skimmers already corroborated.
        # Closes the solo-precision gap by anchoring confidence in
        # what other RBN-feeder skimmers are also hearing.
        rbc = recent_band_config or {}
        self._rb_window_sec = float(rbc.get('window_sec', 3600))
        self._rb_min_spotters = int(rbc.get('min_spotters', 2))
        # _rb_support: {(call_bucket, band_id): {spotter_id: last_seen_ts}}
        self._rb_support = defaultdict(dict)
        self._rb_lock = threading.Lock()
        self._rb_peers_cfg = list(rbc.get('peers', []))
        # Tee threads start lazily on first peer-required call (or via
        # explicit start_recent_band_tees()) so unit-test paths don't
        # spawn networking.

        # Harmonic suppression history. {call_bucket: [(freq_khz, snr, ts), ...]}.
        # Receiver intermod / mixer products generate apparent "spots" at
        # 2x-5x integer multiples of strong fundamentals. The spurs decode
        # to the same callsign as the fundamental (because they are the
        # same audio signal, just at the wrong RF frequency). Filter by
        # checking whether each new emit is a recent same-call spot's
        # harmonic at appropriately weaker SNR.
        self._harm_history = defaultdict(list)
        self._harm_window_sec    = 300    # remember fundamentals for 5 min
        self._harm_max_multiple  = 5      # check 2x-5x harmonics
        self._harm_freq_tol_hz   = 100    # tolerance for the multiple match
        self._harm_min_delta     = 6      # 2nd harmonic must be ≥6 dB weaker
        self._harm_delta_step    = 2      # +2 dB margin per multiple beyond 2
        # 0 = naked (no SCP), None = SCP-only (no bypass), N>0 = promote after N decodes
        self.scp_bypass_threshold = scp_bypass_threshold
        # patt3ch.lst: SkimSrv's structural-pattern allowlist used as the
        # bypass-tier validation. Calls matching a pattern are
        # "structurally legit" — relaxed gate. Calls NOT matching are
        # likely noise (suffixes/fragments of real calls).
        # Format: "<flag> <pattern>" where @=letter, #=digit, literal else.
        # Roughly half are flagged "+" (common) vs unflagged (rare).
        self._patt3ch_by_len_active = {}   # length -> [compiled regex, ...]
        self._patt3ch_by_len_rare   = {}
        self._load_patt3ch(patt3ch_path)

        # Exact match tracking
        self._tracking = defaultdict(lambda: {
            'freq': 0, 'count': 0, 'last_spotted': 0, 'snr': 0
        })
        # Non-SCP callsign bypass: (call, freq_bin) → decode count
        self._bypass_counts = defaultdict(int)
        # Bypass calls already emitted — emit once per (call, freq_bin)
        self._bypass_spotted = set()
        # Per-frequency sighting counts: (call, freq_bin) → count
        self._freq_sightings = defaultdict(int)

        # Temporal fragment accumulation per frequency bin (100 Hz resolution)
        # freq_bin -> {fragment: count}
        self._freq_fragments = defaultdict(lambda: defaultdict(int))
        self._freq_last_seen = defaultdict(float)

        # Cross-channel hallucination filter
        self._cycle_calls = defaultdict(set)

        # Per-freq-bin sighting leader. Only the leader may emit a spot
        # at a given freq_bin within the recent-sightings window. Closes
        # the multi-call-per-freq FP pattern Grayline measured 2026-04-25:
        # 14038.8 was producing 6 different SCP-valid spots from 1 real
        # signal (K2NV, K2YG, VE3GMZ, VE6RST, WB2FUE, WI5D). Tracked
        # as freq_bin -> (call, sighting_count).
        self._freq_leader = {}
        self._freq_committed = {}      # freq_bin -> (call, commit_time)
        self._freq_sighting_times = defaultdict(list)  # freq_bin -> [(t, call), ...]

        # Build SCP prefix index for fast fuzzy matching
        self._scp_by_len = defaultdict(list)
        for call in valid_calls:
            self._scp_by_len[len(call)].append(call)

        # Suffix index for leading-letter substitution recovery.
        # The Bayesian decoder routinely garbles the first character of weak
        # signals (K→T, G→D, H→S, F→E, etc. — they're 1-2 dits apart in Morse).
        # Indexing valid_calls by call[1:] lets us recover the right SCP call
        # in O(1) when there's exactly one match: e.g. "T1LZ" → suffix "1LZ" →
        # uniquely "K1LZ".  Skipped when ambiguous to avoid false rewrites.
        self._scp_by_suffix = defaultdict(list)
        # Prefix index for trailing-letter truncation recovery.
        # The decoder also drops the trailing character routinely (EI5KF→EI5K,
        # DF7TV→DF7T).  When exactly one SCP call extends our candidate by one
        # letter, recover it.  Extensions are usually ambiguous (EI5K → EI5KF
        # / EI5KG / EI5KI / ...), so this fires less often than leading-letter
        # but the same force-bypass gate keeps wrong recoveries safe.
        self._scp_by_prefix = defaultdict(list)
        for call in valid_calls:
            if 4 <= len(call) <= 7 and '/' not in call:
                self._scp_by_suffix[call[1:]].append(call)
            if 5 <= len(call) <= 7 and '/' not in call:
                self._scp_by_prefix[call[:-1]].append(call)

    # Window for time-gated sighting counts (seconds). A call must be
    # seen N times within this window to pass the sightings threshold.
    # Without this, cumulative counts in live mode let noise fragments
    # pass the threshold over minutes/hours.
    SIGHTING_WINDOW = 60.0

    # WPM cap — spots with decoder WPM above this are suppressed as noise.
    # Contest CW tops out ~40-45 WPM; anything above 50 is almost certainly
    # a decoder artifact from noise or digital mode interference.
    MAX_WPM = 50

    # Total dit+dah count per character — low-weight chars are easy to
    # generate from noise; high-weight chars require longer, distinctive patterns.
    _MORSE_WEIGHT = {
        'E': 1, 'T': 1,
        'I': 2, 'A': 2, 'N': 2, 'M': 2,
        'S': 3, 'U': 3, 'R': 3, 'W': 3, 'D': 3, 'K': 3, 'G': 3, 'O': 3,
        'H': 4, 'V': 4, 'F': 4, 'L': 4, 'P': 4, 'J': 4, 'B': 4, 'X': 4,
        'C': 4, 'Y': 4, 'Z': 4, 'Q': 4,
        '0': 5, '1': 5, '2': 5, '3': 5, '4': 5,
        '5': 5, '6': 5, '7': 5, '8': 5, '9': 5,
    }

    @classmethod
    def _morse_weight(cls, call):
        """Total dit+dah elements for a callsign. Low weight = easier to noise-decode."""
        return sum(cls._MORSE_WEIGHT.get(c, 3) for c in call)

    def _min_sightings(self, call, snr=0):
        """Morse-weight-based sighting threshold.
        Low-weight calls (short Morse elements) require more sightings —
        they're generated by noise far more often than high-weight calls.
        Bumped 2026-04-25 after live UK/EI contest produced 87 FPs vs SDC's
        70 unique spots in 20 min — the prior thresholds (6/4/3/2) let too
        many noise-decoded SCP-valid calls through."""
        if call in self.add_calls:
            return 1  # pre-validated rare calls: one clean decode is enough
        # Strip slash suffix for weight calculation
        base = call.split('/')[0]
        w = self._morse_weight(base)
        if w <= 9:    # R3ER=8, N5ER=9, MM2T=9, SE5E=9
            return 7
        elif w <= 12: # K3WW=11, N4BA=11, W4SPR=12
            return 5
        elif w <= 15: # W6AYC=14, KG9X=13, WB2AA=15
            return 4
        return 3

    # ----------------------------------------------------------------
    # Recent-on-band support floor (S-floor) — N2WQ GoCluster port
    # ----------------------------------------------------------------

    # ham bands keyed by lower-edge kHz; value is the band integer label
    _BAND_EDGES = (
        (1800,  160), (3500,   80), (7000,   40), (10100,  30),
        (14000, 20),  (18068,  17), (21000,  15), (24890,  12),
        (28000, 10),  (50000,   6),
    )

    @classmethod
    def _band_id_for_freq(cls, freq_khz):
        """Return the meter-band integer for a frequency. None if out of band."""
        for edge_khz, band in reversed(cls._BAND_EDGES):
            if freq_khz >= edge_khz:
                return band
        return None

    def _ingest_support(self, call, freq_khz, spotter, ts):
        """Record that `spotter` saw `call` on the band of `freq_khz` at `ts`.
        Thread-safe; called from peer-tee threads and from the main loop's
        self-anchor on every confirmed spot."""
        bucket = self._scp_bucket(call)
        band = self._band_id_for_freq(freq_khz)
        if band is None:
            return
        with self._rb_lock:
            self._rb_support[(bucket, band)][spotter] = ts

    def _has_recent_band_support(self, call, freq_khz, now):
        """True if `call` has been seen by >= min_spotters distinct spotters
        on this band within `_rb_window_sec`. Self ('OS:self') counts as a
        spotter, so once we've confirmed the call once it's anchored for
        the window."""
        band = self._band_id_for_freq(freq_khz)
        if band is None:
            return False
        bucket = self._scp_bucket(call)
        cutoff = now - self._rb_window_sec
        with self._rb_lock:
            spotters = self._rb_support.get((bucket, band), {})
            live = sum(1 for ts in spotters.values() if ts >= cutoff)
        return live >= self._rb_min_spotters

    def _effective_min_sightings(self, call, freq_khz, snr=0, now=None):
        """min_sightings, lowered to 1 when the call is anchored by recent
        peer support on this band and the gate flag is on. Otherwise
        identical to _min_sightings(call, snr)."""
        base = self._min_sightings(call, snr)
        if not self.gate_config.get('gate_recent_band_floor'):
            return base
        if now is None:
            now = time.time()
        if self._has_recent_band_support(call, freq_khz, now):
            return 1
        return base

    # Peer-spot wire format. Matches "DX de SPOTTER-#: 14025.50 CALL ..."
    # both with and without our sg_tee timestamp prefix.
    _PEER_SPOT_RE = re.compile(
        r'^(?:\d{2}:\d{2}:\d{2}\s+)?DX de (\S+):\s+(\d+\.\d+)\s+([A-Z0-9/]{3,15})\s+'
    )

    def _peer_connect_loop(self, host, port, label, login_call='WF8Z'):
        """Connect to a peer DX cluster, parse DX lines, ingest as S-floor
        support evidence. Reconnects on disconnect. Runs as daemon thread.

        Recv timeout is generous (10 min) because peer DX clusters can go
        silent for several minutes during quiet hours. SDC in particular
        has a consistent ~126s gap pattern; a 120s recv timeout would
        churn-disconnect every few minutes."""
        while True:
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(600)
                s.connect((host, port))
                time.sleep(0.5)
                try: s.recv(8192)  # banner
                except Exception: pass
                s.sendall(f'{login_call}\n'.encode('ascii'))
                log.info('S-floor: connected to %s peer %s:%d', label, host, port)
                buf = b''
                while True:
                    chunk = s.recv(8192)
                    if not chunk:
                        break
                    buf += chunk
                    while b'\n' in buf:
                        line, buf = buf.split(b'\n', 1)
                        text = line.decode('latin-1', errors='replace').rstrip('\r\x07 \t')
                        m = self._PEER_SPOT_RE.match(text)
                        if not m:
                            continue
                        spotter, freq, call = m.groups()
                        self._ingest_support(
                            call.upper(), float(freq),
                            f'{label}:{spotter}', time.time()
                        )
            except (socket.error, OSError) as e:
                log.debug('S-floor %s peer error: %s', label, e)
            finally:
                if s:
                    try: s.close()
                    except Exception: pass
            time.sleep(5)

    def start_recent_band_tees(self):
        """Spawn one daemon thread per configured peer. Idempotent."""
        if getattr(self, '_rb_peer_threads_started', False):
            return
        self._rb_peer_threads_started = True
        for peer in self._rb_peers_cfg:
            t = threading.Thread(
                target=self._peer_connect_loop,
                args=(peer['host'], peer['port'], peer.get('label', 'PEER')),
                name=f"rb_peer_{peer.get('label','peer')}",
                daemon=True,
            )
            t.start()

    # ----------------------------------------------------------------
    # Harmonic suppression — N2WQ GoCluster port (technique 2 of 4)
    # ----------------------------------------------------------------

    def _is_harmonic_of_recent(self, call, freq_khz, snr, now):
        """True if (call, freq) looks like a 2x-5x harmonic of a recent
        same-call spot at appropriately weaker SNR. Match criteria:

          - Same call (compared by SCP bucket so noise variants align)
          - Fundamental seen within `_harm_window_sec`
          - freq_khz / fund_freq within `_harm_freq_tol_hz` of an
            integer 2..max_multiple
          - This spot's SNR weaker than fundamental by at least
            `_harm_min_delta + (n-2)*_harm_delta_step` dB
            (so 2nd harmonic must be ≥6 dB weaker, 3rd ≥8 dB, etc.)
        """
        bucket = self._scp_bucket(call)
        history = self._harm_history.get(bucket)
        if not history:
            return False
        cutoff = now - self._harm_window_sec
        # Prune in place — cheap and keeps storage bounded.
        history[:] = [h for h in history if h[2] >= cutoff]
        for fund_freq, fund_snr, _ts in history:
            if fund_freq <= 0 or freq_khz <= fund_freq:
                continue
            ratio = freq_khz / fund_freq
            tol_khz = self._harm_freq_tol_hz / 1000.0
            for n in range(2, self._harm_max_multiple + 1):
                if abs(freq_khz - n * fund_freq) <= tol_khz:
                    required = self._harm_min_delta + (n - 2) * self._harm_delta_step
                    if (fund_snr - snr) >= required:
                        return True
                    # Found the multiple but SNR delta wasn't enough — could
                    # legitimately be a different real station. Don't mark.
                    return False
        return False

    def _record_fundamental(self, call, freq_khz, snr, now):
        """Add this spot to the call's harmonic-history list.
        Only call AFTER confirming it is NOT itself a harmonic
        (otherwise we'd record a harmonic as a fundamental, which
        could chain-suppress real spots later)."""
        bucket = self._scp_bucket(call)
        self._harm_history[bucket].append((freq_khz, snr, now))

    def _harmonic_check(self, call, freq_khz, snr, now):
        """Combined check + telemetry. Returns True if the spot
        should be suppressed as a harmonic (caller should `continue`)."""
        if not self._is_harmonic_of_recent(call, freq_khz, snr, now):
            return False
        if self.gate_config.get('gate_harmonic_filter'):
            log.info("HARMONIC suppressed: %s @ %.1f kHz snr=%d",
                     call, freq_khz, snr)
            return True
        elif self.gate_config.get('gate_telemetry'):
            log.info("PHANTOM harmonic: %s @ %.1f kHz snr=%d would suppress",
                     call, freq_khz, snr)
        return False

    def _load_patt3ch(self, path):
        """Load SkimSrv's patt3ch.lst structural-pattern allowlist.
        Compiles each pattern to a regex and indexes by call length for
        O(patterns_at_length) lookup. Patterns flagged "+" go in the
        active bucket; unflagged go in rare. Both are valid; flag is
        used to weight confidence."""
        try:
            with open(path) as fp:
                for line in fp:
                    line = line.rstrip()
                    if len(line) < 2:
                        continue
                    flag = line[0]
                    pat = line[2:].strip()
                    if not pat:
                        continue
                    rx_str = '^' + pat.replace('@', '[A-Z]').replace('#', '[0-9]') + '$'
                    rx = re.compile(rx_str)
                    bucket = (self._patt3ch_by_len_active if flag == '+'
                              else self._patt3ch_by_len_rare)
                    bucket.setdefault(len(pat), []).append(rx)
            n_active = sum(len(v) for v in self._patt3ch_by_len_active.values())
            n_rare = sum(len(v) for v in self._patt3ch_by_len_rare.values())
            log.info("Loaded patt3ch.lst: %d active + %d rare patterns",
                     n_active, n_rare)
        except FileNotFoundError:
            log.warning("patt3ch.lst not found at %s — bypass-tier validation disabled", path)

    def _matches_patt3ch(self, call):
        """Return 'active' if call matches a "+"-flagged pattern,
        'rare' if it matches an unflagged pattern, or None."""
        n = len(call)
        for rx in self._patt3ch_by_len_active.get(n, []):
            if rx.match(call):
                return 'active'
        for rx in self._patt3ch_by_len_rare.get(n, []):
            if rx.match(call):
                return 'rare'
        return None

    # Deferred consensus emission: a freq_bin must accumulate this many
    # sightings (across all candidate calls) before we commit to one call
    # and emit it. Suppresses the multiple-spots-from-one-real-signal
    # pattern (K4R / K4RU / K4RUM / K4RUMN / TT5CM / K5CM all from one
    # K4RUM CQ). Once committed, other calls at this freq are silenced
    # for COMMIT_LOCK_SEC. Reset when committed call hasn't been re-sighted
    # in COMMIT_LOCK_SEC (runner abandoned/QSY).
    COMMIT_THRESHOLD = 5
    COMMIT_LOCK_SEC = 300

    _BUCKET_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

    # Leading-noise trim characters: E (.), T (-), I (..), S (...) are the
    # shortest Morse symbols and the most common decoder noise prefixes
    # (the decoder hears noise crackle as a few short elements before the
    # real signal starts). Trimming ≤2 of these from the front and
    # re-checking SCP catches ITM4FO/EVM4FO/etc that edit-1 alone misses.
    _BUCKET_NOISE_CHARS = 'ETIS'

    def _scp_bucket(self, call):
        """Map a call to its consensus bucket: nearest SCP-valid call within
        edit distance 1 (substitution), or after trimming ≤2 leading
        noise-prefix chars (E/T/I/S) plus another edit-1 substitution.
        Returns the call itself if no unambiguous SCP neighbor exists.
        Collapses garbled variants (VM4FO, ITM4FO, EVM4FO → KM4FO) into
        one vote bucket while keeping distinct SCP-valid calls (K8MA vs
        K8MR) separate. Cached per-call (deterministic mapping)."""
        if not hasattr(self, '_bucket_cache'):
            self._bucket_cache = {}
        if call in self._bucket_cache:
            return self._bucket_cache[call]
        if call in self.valid_calls:
            self._bucket_cache[call] = call
            return call

        def _edit1_unique_scp(s):
            """Return the unique SCP-valid edit-1 substitution of s, or
            None if zero or 2+ matches (ambiguous → no merge)."""
            matches = set()
            for i in range(len(s)):
                for c in self._BUCKET_CHARS:
                    if c == s[i]: continue
                    cand = s[:i] + c + s[i+1:]
                    if cand in self.valid_calls:
                        matches.add(cand)
                        if len(matches) > 1:
                            return None
            return matches.pop() if len(matches) == 1 else None

        bucket = _edit1_unique_scp(call)
        if bucket:
            self._bucket_cache[call] = bucket
            return bucket

        # TEST<call> fusion split: when the runner sends "TEST <call>" with
        # too little inter-word space, the decoder fuses them and emits
        # "T<call>". The leading "T" is a single dah from the trailing K
        # of "TEST" or just the gap collapse. If stripping a leading T
        # yields a SCP-valid call, that's almost certainly what happened.
        # Industry-known issue (RBN-OPS thread 2023-09-20, N4ZR's example).
        # Only fires when call is 5+ chars (TN4ZR → N4ZR is the canonical
        # case) and the stripped form is in SCP exactly.
        if len(call) >= 5 and call[0] == 'T':
            suffix = call[1:]
            if suffix in self.valid_calls:
                self._bucket_cache[call] = suffix
                return suffix

        # Try trimming ≤2 leading noise chars (E/T/I/S) and looking for
        # an EXACT SCP match.  We do NOT chain edit-1 after trim: that
        # combination was too aggressive and produced country-prefix
        # collapses (IT9DV→R9DV, IR3OR→R3OR — Italian prefixes turning
        # into Russian via "trim I, sub T→R").  Many real DX prefixes
        # start with E or T (EA/EI/ES Spain/Ireland/Estonia, IT/IR/IS/
        # IZ Italy, TF/TI/TG Iceland/CR/Guatemala).  Trim+exact preserves
        # the legitimate "leading-noise prefix on a real call" recovery
        # while preventing the country flip damage.
        for trim_len in range(1, 3):
            if len(call) - trim_len < 3: break
            if not all(c in self._BUCKET_NOISE_CHARS for c in call[:trim_len]):
                break
            suffix = call[trim_len:]
            if suffix in self.valid_calls:
                self._bucket_cache[call] = suffix
                return suffix

        # No SCP neighbor — vote for self (could be special event, club
        # call, or noise; decisions about acceptance happen downstream)
        self._bucket_cache[call] = call
        return call

    def _record_sighting(self, call, freq_bin, now):
        """Record a timestamped sighting for time-windowed counting.
        Maintains both per-call and per-freq indices for fast lookup
        from either side. Sightings are recorded under the SCP bucket
        (garbled variants collapse to nearest SCP) so the consensus
        winner counts votes from all decode variants."""
        if not hasattr(self, '_sighting_times'):
            self._sighting_times = defaultdict(list)
        if not hasattr(self, '_freq_sighting_times'):
            self._freq_sighting_times = defaultdict(list)
        bucket = self._scp_bucket(call)
        self._sighting_times[bucket].append((now, freq_bin))
        self._freq_sighting_times[freq_bin].append((now, bucket))

    def _count_freq_total(self, freq_bin, now):
        """Total sightings at freq_bin across ALL calls within SIGHTING_WINDOW."""
        if not hasattr(self, '_freq_sighting_times'):
            return 0
        cutoff = now - self.SIGHTING_WINDOW
        fresh = [(t, c) for t, c in self._freq_sighting_times.get(freq_bin, [])
                 if t >= cutoff]
        self._freq_sighting_times[freq_bin] = fresh
        return len(fresh)

    def _freq_winner(self, freq_bin, now):
        """Call with the most sightings at freq_bin within SIGHTING_WINDOW.
        Returns (call, count) or (None, 0) if no sightings."""
        if not hasattr(self, '_freq_sighting_times'):
            return None, 0
        cutoff = now - self.SIGHTING_WINDOW
        from collections import Counter
        counts = Counter(c for t, c in self._freq_sighting_times.get(freq_bin, [])
                         if t >= cutoff)
        if not counts:
            return None, 0
        call, n = counts.most_common(1)[0]
        return call, n

    def _adaptive_commit_threshold(self, freq_bin, now):
        """Adapt the consensus threshold based on freq clutter (number of
        distinct candidate calls in the last SIGHTING_WINDOW). With SCP
        bucketing in place, garbled variants (VM4FO, ITM4FO) collapse
        into their nearest SCP-valid bucket (KM4FO), so 'distinct' here
        means distinct *real* calls, not decode variants.
          1 distinct → 2 sightings   (clean POTA/SST/casual op — fast)
          2-3 distinct → 3 sightings (light competition)
          4+ distinct → 5 sightings  (pile-up / contest / heavy spread)"""
        if not hasattr(self, '_freq_sighting_times'):
            return 2
        cutoff = now - self.SIGHTING_WINDOW
        distinct = {c for t, c in self._freq_sighting_times.get(freq_bin, [])
                    if t >= cutoff}
        n = len(distinct)
        if n <= 1: return 2   # clean — POTA/SST single op
        if n <= 3: return 3   # light competition
        return 5              # pile-up / contest / heavy spread

    def _committed_call_alive(self, call, freq_bin, now):
        """True iff committed call has been re-sighted within COMMIT_LOCK_SEC.
        When false, the freq slot is released (runner QSY/abandoned).
        Bucket-aware lookup (matches _record_sighting's storage key)."""
        if not hasattr(self, '_sighting_times'):
            return False
        bucket = self._scp_bucket(call)
        cutoff = now - self.COMMIT_LOCK_SEC
        for t, fb in self._sighting_times.get(bucket, []):
            if fb == freq_bin and t >= cutoff:
                return True
        return False

    # Max distinct frequency bins within the sighting window before
    # suppressing a call as a noise hallucination. Real stations sit on
    # one frequency; noise fragments scatter across the band.
    MAX_FREQ_SPREAD = 3

    # Characters that are short Morse elements — callsigns composed
    # entirely of these are highly susceptible to noise decode.
    SHORT_MORSE_CHARS = set('EISTR5H')

    def _count_recent_sightings(self, call, freq_bin, now):
        """Count sightings within SIGHTING_WINDOW seconds at the same freq bin.
        Suppresses calls seen at too many distinct frequencies (noise scatter).

        Bucket-aware: _record_sighting stores entries under the SCP bucket
        (so VM4FO/ITM4FO/KM4FO all collapse to one KM4FO key). The lookup
        here must use the same bucketing so garbled raw calls find the
        accumulated count of their bucket family. Without this, garbled
        forms returned 0 sightings even after their bucket had accumulated
        many votes — silently killing the multi-sighting gate path."""
        if not hasattr(self, '_sighting_times'):
            return 0
        bucket = self._scp_bucket(call)  # match _record_sighting's storage key
        entries = self._sighting_times.get(bucket, [])
        cutoff = now - self.SIGHTING_WINDOW
        fresh = [(t, fb) for t, fb in entries if t >= cutoff]
        self._sighting_times[bucket] = fresh

        # Multi-frequency suppression: if this call appears at 3+ distinct
        # freq bins in the window, it's noise — real ops don't QSY 3x/min.
        distinct_bins = set(fb for _, fb in fresh)
        if len(distinct_bins) > self.MAX_FREQ_SPREAD:
            return 0

        return sum(1 for _, fb in fresh if fb == freq_bin)

    def _is_freq_leader(self, call, freq_bin, now):
        """Returns True iff `call` has the most recent sightings at
        `freq_bin` within SIGHTING_WINDOW. Suppresses non-leader calls
        so each freq_bin emits at most one spot per window — closes the
        multi-call-per-freq FP pattern (one real signal producing 5+
        SCP-valid spots from regex matches inside noise-decoded text).

        Updates the per-bin leader cache when this call is or becomes
        the leader. A stale leader (window expired → 0 sightings) is
        replaced by any call with sightings >0."""
        my_count = self._count_recent_sightings(call, freq_bin, now)
        leader = self._freq_leader.get(freq_bin)
        if leader is None:
            self._freq_leader[freq_bin] = (call, my_count)
            return True
        leader_call, _leader_cached = leader
        if leader_call == call:
            self._freq_leader[freq_bin] = (call, my_count)
            return True
        # Different call leads — recompute their current count (cached
        # value can be stale if their sightings have aged out).
        fresh_leader_count = self._count_recent_sightings(
            leader_call, freq_bin, now)
        if fresh_leader_count == 0 or my_count > fresh_leader_count:
            self._freq_leader[freq_bin] = (call, my_count)
            return True
        return False

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

    def _extend_truncated_call(self, call):
        """If `call` looks like a real SCP call with the last letter dropped,
        return the SCP version when uniquely determined.

        Bayesian decoder also routinely drops the trailing character on weak
        signals or when the operator's WPM ramps mid-word.  Live RF (2026-04-26
        05:31 UTC) showed EI5KF being decoded as EI5K and DF7TV as DF7T."""
        if call in self.valid_calls:
            return None
        if len(call) < 4 or len(call) > 6:
            return None
        candidates = self._scp_by_prefix.get(call)
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _correct_leading_letter(self, call):
        """If `call` looks like a leading-letter substitution of a single SCP
        call, return that SCP call.  Returns None if `call` is already in SCP,
        if no SCP variant matches, or if multiple variants match (ambiguous).

        Diagnoses the Bayesian decoder's most common failure on weak signals:
        the first character lands on a Morse-confusable letter.  Examples
        from live SDC diff (2026-04-26 05:04-05:09 UTC):
            T1LZ  → K1LZ    SB9BUN → HB9BUN    E5IN → F5IN
            MM8U  → GM8U    E4HRM  → DL4HRM (2-edit, not handled here)
        """
        if call in self.valid_calls:
            return None
        if len(call) < 4 or len(call) > 7:
            return None
        suffix = call[1:]
        candidates = self._scp_by_suffix.get(suffix)
        if not candidates:
            return None
        # Single-match policy: only correct when unambiguous.  Many SCP suffixes
        # (e.g. "1AW") have dozens of valid prefixes — substituting the first
        # one would be wrong as often as right.
        if len(candidates) == 1:
            return candidates[0]
        return None

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
                dec_type='primary', wpm=0):
        """Process decoded text. Returns list of spot dicts.

        text: new text fragment (1-2 chars in streaming mode)
        context_text: full accumulated text from the decoder instance
        dec_type: 'primary' (uhsdr) or 'secondary' (bmorse/hamfist).
            Secondary decoders only contribute at frequencies where no primary
            decoder has produced an exact SCP match.
        wpm: decoder-estimated WPM (0 = unknown). Carried through to spot dict.
        """
        # Track processed length per (frequency, decoder_id) to avoid re-processing.
        # decoder_id is id(d) from SignalGroup.read() — stable for the decoder's
        # lifetime, unique across concurrent decoders on the same frequency.
        freq_bin = int(round(freq_khz * 2))  # 500 Hz bins — 100 Hz caused bleedthrough from adjacent channels to saturate _cycle_calls
        cache_key = (freq_bin, decoder_id)

        # Track which frequencies have primary exact matches
        if not hasattr(self, '_primary_matched'):
            self._primary_matched = set()  # freq_bins with primary exact SCP match

        # Secondary decoders run freely — sightings threshold is the quality gate
        if not hasattr(self, '_processed_len'):
            self._processed_len = {}

        # WPM cap: suppress spots from decoders reporting unrealistic speed.
        # Noise and digital-mode interference produce high WPM artifacts.
        if wpm > self.MAX_WPM:
            return []

        full_text = context_text or text

        # ITILA emits fresh short strings each window ("CQ CALL "), not an
        # accumulating context.  Skip the incremental-length guard for itila —
        # its own _decode_window seen-set prevents duplicate entries per window.
        if dec_type == 'itila':
            new_text = full_text
        else:
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

        # CQ-runner identification: when CQ context is present, the call
        # adjacent to CQ is the runner; other SCP-valid calls in the same
        # buffer (worked stations during exchanges) are not. RBN cross-check
        # 2026-04-26: 59% of false-positive spots were wrong-call-of-real-
        # signal — a SCP-valid call from the runner's QSO leaking through
        # the has_context gate. Pre-compute the runner once per call to
        # _decode_window so we can demote non-runner SCP matches below.
        recent_ctx_for_runner = context_clean[-500:] if len(context_clean) > 500 else context_clean
        has_context_global = bool(CQ_PATTERNS.search(recent_ctx_for_runner))
        cq_runner_call = (_itila_extract_cq_call(recent_ctx_for_runner, self.valid_calls)
                          if has_context_global else None)
        # Collapse runner candidate to its SCP bucket too, so a garbled
        # form in the text (VM4FO) matches a clean form (KM4FO) in the gate.
        cq_runner_bucket = self._scp_bucket(cq_runner_call) if cq_runner_call else None

        # --- Path 1: Exact SCP match ---
        # 1a: regex scan (word-boundary aware)
        seen_p1 = set()
        for m in CALL_RE.finditer(clean):
            call = m.group(1)
            # 3-char minimum: M7Z, M3A, G6M etc. are real contest calls in SCP.
            if len(call) < 3 or call in FALSE_POSITIVES:
                continue
            if call in self.blacklist:
                continue

            # Strip slash suffix and spot the BASE call only (RBN convention).
            # Live SDC comparison 2026-04-25: SDC's spots have ZERO slashes;
            # they all strip to base. RBN aggregator filters slash spots
            # against MASTER.SCP (base-only) so they're dropped upstream
            # anyway — emitting them is wasted work plus it inflates our
            # FP count with garbled suffixes (N8KH/B, K8MR/HPT, W4DXM/IDR
            # etc., all from decoder bit errors after the base call).
            #
            # Either side of the slash that's a base call in SCP wins.
            # If BOTH sides are SCP base calls (e.g. W4/N1ABC = N1ABC
            # operating from W4 area), prefer the longer one (the actual
            # operator's call, not the regional prefix).
            if '/' in call:
                _parts = call.split('/', 1)
                _candidates = []
                if _is_base_call(_parts[0]) and _parts[0] in self.valid_calls:
                    _candidates.append(_parts[0])
                if _is_base_call(_parts[1]) and _parts[1] in self.valid_calls:
                    _candidates.append(_parts[1])
                if _candidates:
                    call = max(_candidates, key=len)
                # else: leave call as-is, will fail SCP check below
            # Track whether `call` came from a decoder-error correction.
            # Force corrected calls through bypass (multi-sighting) instead
            # of exact (single-shot with context): SCP membership of the
            # corrected call doesn't prove the decoder heard it correctly.
            # Live-RF data (2026-04-26 05:17 UTC) showed Bayesian hallucinating
            # N4UL from a clean DF7TV signal; treating that as "exact" plus
            # context-gated would spot N4UL.  Multi-sighting catches the drift.
            #
            # Two correction modes, both only attempted at sane WPM (decoder
            # output above 45 WPM is almost always noise hallucination):
            #   leading-letter — first char garbled (T1LZ → K1LZ)
            #   prefix-extend  — trailing char dropped (EI5K → EI5KF)
            # Each requires an unambiguous single SCP match, else stay quiet.
            forced_bypass = False
            if '/' not in call and 0 < wpm <= 45:
                _corrected = self._correct_leading_letter(call) \
                          or self._extend_truncated_call(call)
                if _corrected:
                    log.info("SCP correct: %s → %s @ %.1f kHz wpm=%d",
                             call, _corrected, freq_khz, wpm)
                    call = _corrected
                    forced_bypass = True

            # SCP bucket: collapse single-edit garbled variants to nearest
            # SCP-valid call. VM4FO/ITM4FO/EVM4FO → KM4FO. K8MA and K8MR
            # stay distinct (both SCP-valid → no merge). Bucketing always
            # logs the mapping; whether to SUBSTITUTE the call (emit bucket
            # form instead of raw) is controlled by gate_scp_bucket_substitute.
            if '/' not in call:
                _bucket = self._scp_bucket(call)
                if _bucket != call and _bucket in self.valid_calls:
                    log.info("SCP bucket: %s → %s @ %.1f kHz", call, _bucket, freq_khz)
                    if self.gate_config['gate_scp_bucket_substitute']:
                        call = _bucket

            # Re-check blacklist after slash-strip / bucket substitution.
            # Decoder noise can map to a blacklisted call via edit-1
            # bucket substitute (e.g. "K7A" → "C7A", "K0A" → "B0A").
            # Without this re-check the blacklist is bypassed for any
            # call our decoder produces a typo'd version of.
            if call in self.blacklist:
                continue

            # Global WPM sanity gate.  MAX_WPM=50 is defined as a constant
            # documenting "spots above this are almost certainly noise" but
            # was never actually enforced — wire it in here.  Contest CW
            # tops out around 40-45 WPM in practice; 50+ is decoder frenzy.
            if wpm > self.MAX_WPM:
                continue
            if call in self.valid_calls and not forced_bypass:
                seen_p1.add(call)
                # Primary decoder exact match — suppress secondary decoders here
                if dec_type in ('primary', 'itila'):
                    self._primary_matched.add(freq_bin)
                    self._cycle_calls[call].add(freq_bin)
                info = self._tracking[call]
                info['count'] += 1
                info['freq'] = freq_khz
                info['snr'] = max(info['snr'], snr)
                self._record_sighting(call, freq_bin, now)
                recent_count = self._count_recent_sightings(call, freq_bin, now)
                # Context check: only look at RECENT text (last ~500 chars)
                # to avoid "CQ" appearing by chance in hours of noise output.
                recent_ctx = context_clean[-500:] if len(context_clean) > 500 else context_clean
                has_context = bool(CQ_PATTERNS.search(recent_ctx))
                min_s = self._effective_min_sightings(call, freq_khz, snr, now)
                # CQ-runner gate: when a CQ context exists AND we identified
                # the runner adjacent to CQ, only THAT call passes via context.
                # Other SCP-valid calls in the same buffer are likely worked
                # stations from the runner's QSO and must satisfy
                # multi-sighting on their own to be spotted. This kills the
                # 59% wrong-call-of-real-signal failure mode (2026-04-26 RBN
                # cross-check). When no runner could be identified despite
                # has_context (CQ token garbled, no adjacent base call), fall
                # back to original permissive behavior.
                # Bucket-aware comparison: cq_runner_bucket is already a
                # bucket form. Compare against this call's bucket so the
                # gate works whether or not gate_scp_bucket_substitute
                # is on (raw call vs bucket call shouldn't affect runner
                # identification).
                _call_bucket = self._scp_bucket(call)
                if (self.gate_config['gate_cq_runner']
                        and has_context and cq_runner_bucket is not None):
                    gate = (_call_bucket == cq_runner_bucket) or (recent_count >= min_s)
                    if not gate and self.gate_config['gate_telemetry']:
                        log.debug("WOULD-GATE cq_runner: %s @ %.1f kHz "
                                  "(runner=%s, count=%d)",
                                  call, freq_khz, cq_runner_bucket, recent_count)
                else:
                    gate = has_context or recent_count >= min_s
                    # Telemetry: log what cq_runner WOULD have done if enabled
                    if (self.gate_config['gate_telemetry']
                            and has_context and cq_runner_bucket is not None
                            and _call_bucket != cq_runner_bucket
                            and recent_count < min_s):
                        log.debug("PHANTOM cq_runner: %s @ %.1f kHz would suppress "
                                  "(runner=%s)", call, freq_khz, cq_runner_bucket)

                # Deferred consensus gate: emit at most one call per freq_bin,
                # picked by majority sightings after COMMIT_THRESHOLD total
                # sightings have accumulated at that freq across all calls.
                # Suppresses the multiple-spots-from-one-real-signal pattern
                # where K4R/K4RU/K4RUM/K4RUMN/TT5CM all get emitted from one
                # K4RUM CQ. Once a freq commits to a call, other calls there
                # are silenced for COMMIT_LOCK_SEC; reset when the committed
                # call hasn't been re-sighted in COMMIT_LOCK_SEC.
                if gate and self.gate_config['gate_freq_consensus']:
                    committed = self._freq_committed.get(freq_bin)
                    if committed:
                        committed_call, _ = committed
                        if not self._committed_call_alive(committed_call, freq_bin, now):
                            del self._freq_committed[freq_bin]
                            committed = None
                        elif call != committed_call:
                            gate = False  # suppress non-committed call at this freq
                    if gate and not committed:
                        total_at_freq = self._count_freq_total(freq_bin, now)
                        thresh = self._adaptive_commit_threshold(freq_bin, now)
                        if total_at_freq < thresh:
                            gate = False  # not enough evidence yet — defer
                        else:
                            winner_call, winner_n = self._freq_winner(freq_bin, now)
                            if winner_call != call:
                                gate = False  # this call isn't the winner
                            else:
                                self._freq_committed[freq_bin] = (call, now)
                                log.info("FREQ COMMIT: %s @ bin=%d (%d/%d sightings, thresh=%d)",
                                         call, freq_bin, winner_n, total_at_freq, thresh)

                if gate and self._can_respot(call, freq_khz, now) \
                        and self._is_freq_leader(call, freq_bin, now):
                    if len(self._cycle_calls[call]) < 3:  # hallucination check
                        if self._harmonic_check(call, freq_khz, snr, now):
                            continue
                        self._mark_spotted(call, freq_khz, now)
                        spots.append({
                            'call': call,
                            'freq_khz': freq_khz,
                            'snr': snr,
                            'wpm': wpm,
                            'method': 'exact',
                        })
                        self._ingest_support(call, freq_khz, 'OS:self', now)
                        self._record_fundamental(call, freq_khz, snr, now)
            elif self.scp_bypass_threshold and CALL_RE.match(call) \
                    and call not in seen_p1:
                # Non-SCP structurally-valid call. Two layered behaviors,
                # each flag-controlled:
                #   gate_patt3ch_filter — drop calls not matching patt3ch.lst
                #   gate_bypass_consensus — bypass goes through freq consensus
                # When both off (ship-it default): emit on simple count
                # threshold, tag with patt3ch result for downstream visibility.
                seen_p1.add(call)  # dedupe within this process() call
                if wpm > self.MAX_WPM:
                    continue
                patt3ch_match = self._matches_patt3ch(call)  # 'active' / 'rare' / None
                if self.gate_config['gate_patt3ch_filter'] and patt3ch_match is None:
                    # Drop bypass calls not matching any common pattern.
                    # Likely noise — fragments / contest-exchange garbage.
                    continue
                elif (patt3ch_match is None
                      and self.gate_config['gate_telemetry']):
                    log.debug("PHANTOM patt3ch_filter: %s @ %.1f kHz "
                              "would suppress (no pattern match)",
                              call, freq_khz)
                self._record_sighting(call, freq_bin, now)

                bypass_gate = True
                if self.gate_config['gate_bypass_consensus']:
                    # Per-freq consensus + lock for bypass. Tighter floor:
                    # patt3ch active ("+") = 3 sightings, rare = 5.
                    committed = self._freq_committed.get(freq_bin)
                    if committed:
                        committed_call, _ = committed
                        if not self._committed_call_alive(committed_call, freq_bin, now):
                            del self._freq_committed[freq_bin]
                            committed = None
                        elif call != committed_call:
                            bypass_gate = False
                    if bypass_gate and not committed:
                        total_at_freq = self._count_freq_total(freq_bin, now)
                        base_thresh = self._adaptive_commit_threshold(freq_bin, now)
                        floor = 2 if patt3ch_match == 'active' else 5
                        thresh = max(base_thresh, floor)
                        if total_at_freq < thresh:
                            bypass_gate = False
                        else:
                            winner_call, winner_n = self._freq_winner(freq_bin, now)
                            if winner_call != call:
                                bypass_gate = False
                            else:
                                self._freq_committed[freq_bin] = (call, now)
                                log.info("FREQ COMMIT (bypass/%s): %s @ bin=%d (%d/%d sightings, thresh=%d)",
                                         patt3ch_match, call, freq_bin, winner_n, total_at_freq, thresh)
                else:
                    # Simple count-threshold path (pre-tonight behavior):
                    # promote when same (call, freq_kHz_bucket) has been
                    # decoded N times, where N = scp_bypass_threshold.
                    bypass_freq = int(round(freq_khz))
                    bkey_count = (call, bypass_freq)
                    self._bypass_counts[bkey_count] += 1
                    if self._bypass_counts[bkey_count] < self.scp_bypass_threshold:
                        bypass_gate = False

                bkey = (call, int(round(freq_khz)))
                if bypass_gate and bkey not in self._bypass_spotted \
                        and self._can_respot(call, freq_khz, now):
                    if self._harmonic_check(call, freq_khz, snr, now):
                        continue
                    self._bypass_spotted.add(bkey)
                    log.info("SCP bypass: %s at %.1f kHz [patt3ch=%s]",
                             call, freq_khz, patt3ch_match or 'none')
                    self._mark_spotted(call, freq_khz, now)
                    spots.append({
                        'call': call,
                        'freq_khz': freq_khz,
                        'snr': snr,
                        'wpm': wpm,
                        'method': 'unverified',
                    })
                    self._ingest_support(call, freq_khz, 'OS:self', now)
                    self._record_fundamental(call, freq_khz, snr, now)

        # Path 1b (sliding window on collapsed text) DISABLED 2026-04-25.
        # It scanned every 4-7 char window across the no-spaces decoded text
        # and snap-matched against the ~50K SCP entries. With that many calls
        # in the database, random 4-5 char substrings inside noisy decoder
        # output match valid SCP calls often enough to slip past the
        # sightings gate. Live UK/EI DX 17:30 UTC: 14038.8 kHz produced
        # 5 different SCP-valid spots (N0HJZ, N5KW, VE7ZO, W0PI, WF3T) from
        # ONE real signal — Path 1b was the source. Path 1a (regex with
        # word boundaries) catches real calls fine; the "embedded in noise"
        # recall recovery isn't worth the precision cost.

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
                    if self._can_respot(consensus_str, freq_khz, now) \
                            and self._is_freq_leader(consensus_str, freq_bin, now):
                        if consensus_str not in self.blacklist:
                            self._mark_spotted(consensus_str, freq_khz, now)
                            info['freq'] = freq_khz
                            info['snr'] = snr
                            if self._harmonic_check(consensus_str, freq_khz, snr, now):
                                frags_at_freq.clear()
                                continue
                            spots.append({
                                'call': consensus_str,
                                'freq_khz': freq_khz,
                                'snr': snr,
                                'wpm': wpm,
                                'method': f'consensus(n={len(frag_list)},conf={avg_confidence:.0%})',
                            })
                            self._ingest_support(consensus_str, freq_khz, 'OS:self', now)
                            self._record_fundamental(consensus_str, freq_khz, snr, now)
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
                        if self._can_respot(best_call, freq_khz, now) \
                                and self._is_freq_leader(best_call, freq_bin, now):
                            if best_call not in self.blacklist:
                                self._mark_spotted(best_call, freq_khz, now)
                                info['freq'] = freq_khz
                                info['snr'] = snr
                                if self._harmonic_check(best_call, freq_khz, snr, now):
                                    frags_at_freq.clear()
                                    continue
                                spots.append({
                                    'call': best_call,
                                    'freq_khz': freq_khz,
                                    'snr': snr,
                                    'wpm': wpm,
                                    'method': f'fuzzy_consensus(d={best_dist},n={len(frag_list)},conf={avg_confidence:.0%})',
                                })
                                self._ingest_support(best_call, freq_khz, 'OS:self', now)
                                self._record_fundamental(best_call, freq_khz, snr, now)
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


class SparkGap:
    """Main daemon — streaming architecture with dynamic decoder instances."""

    def __init__(self, config):
        self.cfg = config
        self.receiver = None
        self.manager  = None   # single-band legacy ref (= self.managers[0])
        self.managers  = []    # one InstanceManager per band
        self._band_meta = []   # [(name, center_hz, rx_index), ...]
        self.tracker = None
        self.telnet = None
        self.running = False
        self.spot_count = 0
        self.start_time = None
        self._iq_lock = threading.Lock()
        # Per-band IQ buffers keyed by rx_index.  For single-band configs
        # rx_index=0 is the only key (same as before).
        self._band_bufs = {}   # rx_index → {'iq': deque, 'i': [], 'q': []}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_band(band_entry):
        """Return (name, center_hz) from a bands[] config entry.

        Accepts:
          '40m'             → ('40m', 7100000)
          7090000           → ('7090', 7090000)
          {'center_khz': 7090, 'rx_index': 0}  → ('7090', 7090000)
          {'center_hz': 7090000}                → ('7090000', 7090000)
        """
        if isinstance(band_entry, dict):
            if 'center_khz' in band_entry:
                hz = int(band_entry['center_khz'] * 1000)
            elif 'center_hz' in band_entry:
                hz = int(band_entry['center_hz'])
            else:
                raise ValueError(f"Band dict must have center_khz or center_hz: {band_entry}")
            name = band_entry.get('name', str(int(hz // 1000)))
            return name, hz
        if isinstance(band_entry, str) and band_entry in BANDS:
            return band_entry, BANDS[band_entry]
        return str(band_entry), int(band_entry)

    async def start(self):
        self.start_time = time.time()

        self._add_calls_path = self.cfg.get('add_calls', 'add_calls.txt')
        spot_log_path = self.cfg.get('spot_log')
        self._spot_log = open(spot_log_path, 'a', buffering=1) if spot_log_path else None

        self._wav_record = None
        record_wav = self.cfg.get('record_wav')
        if record_wav:
            import wave as _wave, datetime as _dt
            ts = _dt.datetime.utcnow().strftime('%Y%m%d_%H%M%SZ')
            band_khz = self.cfg.get('bands', [0])[0] // 1000
            rec_path = record_wav.format(ts=ts, band=band_khz)
            self._wav_record = _wave.open(rec_path, 'wb')
            self._wav_record.setnchannels(2)
            self._wav_record.setsampwidth(2)
            self._wav_record.setframerate(self.cfg.get('sample_rate', 192000))
            log.info("Recording IQ to %s", rec_path)
        calls, blacklist, add_calls = load_callsign_db(
            self.cfg.get('master_scp', 'MASTER.SCP'),
            self._add_calls_path,
            self.cfg.get('blacklist', 'blacklist.txt'),
        )
        self._add_calls_mtime = (os.path.getmtime(self._add_calls_path)
                                 if os.path.exists(self._add_calls_path) else 0)
        # Build gate_config from any of the gate_* / enable_* keys in cfg.
        # SpotTracker fills in any missing keys from GATE_DEFAULTS (ship-it
        # mode = all gates off, caller-spotting on).
        gate_config = {k: self.cfg[k] for k in SpotTracker.GATE_DEFAULTS
                       if k in self.cfg}
        self.tracker = SpotTracker(calls, blacklist,
                                   self.cfg.get('respot_interval', 120),
                                   add_calls=add_calls,
                                   scp_bypass_threshold=int(self.cfg.get('scp_bypass_threshold', 0)),
                                   gate_config=gate_config,
                                   recent_band_config=self.cfg.get('recent_band_floor'))
        # If the gate is configured (peers listed), start the peer-tee
        # threads regardless of whether the gate is currently on. The
        # support map is cheap to maintain and we want it warm if the
        # gate is flipped on at runtime via config reload.
        if self.cfg.get('recent_band_floor', {}).get('peers'):
            self.tracker.start_recent_band_tees()

        self.telnet = SpotTelnetServer(
            port=self.cfg.get('telnet_port', 7300),
            callsign=self.cfg.get('callsign', 'WF8Z'),
            node_call=self.cfg.get('node_call', 'SPARK-2'),
            skimmer_suffix=self.cfg.get('skimmer_suffix', '-#'),
            source_tag=self.cfg.get('source_tag', 'SG'),
            op_name=self.cfg.get('op_name', ''),
            qth=self.cfg.get('qth', ''),
            grid=self.cfg.get('grid', ''),
            validation_level=self.cfg.get('validation_level', 'Normal'),
            skimsrv_version=self.cfg.get('skimsrv_version', '1.6.0.145'),
        )
        await self.telnet.start()

        # ----------------------------------------------------------------
        # Band configuration — supports single band (legacy) and multi-band
        # ----------------------------------------------------------------
        bands_cfg = self.cfg.get('bands', ['20m'])
        self._band_meta = []
        for idx, entry in enumerate(bands_cfg):
            name, center_hz = self._resolve_band(entry)
            # rx_index: explicit in dict, else sequential (0, 1, 2, …)
            rx_idx = entry.get('rx_index', idx) if isinstance(entry, dict) else idx
            self._band_meta.append((name, center_hz, rx_idx))
            self._band_bufs[rx_idx] = {
                'iq': deque(),
                'raw': [],   # raw iq_samples lists from receiver thread (no numpy)
                'i': [],
                'q': [],
            }

        # Advertise band ranges in SETT response so Aggregator knows
        # what we cover. Range = center ± half scan bandwidth (kHz).
        half_bw_khz = float(self.cfg.get('skim_bandwidth_khz', 96)) / 2.0
        self.telnet.bands = [
            (round(center_hz / 1000.0 - half_bw_khz, 1),
             round(center_hz / 1000.0 + half_bw_khz, 1))
            for _, center_hz, _ in self._band_meta
        ]

        rx_sample_rate = self.cfg.get('sample_rate', 48000)
        sdr_port = self.cfg.get('sdr_port', 1024)

        sdr_ip = self.cfg.get('sdr_ip')
        if sdr_ip:
            log.info("Using direct SDR IP: %s", sdr_ip)
            device_ip = sdr_ip
        else:
            devices = discover(port=sdr_port)
            if not devices:
                log.error("No HPSDR devices found")
                return False
            device_ip = devices[0]['ip']
        sdr_type = self.cfg.get('sdr_type', 'hpsdr')

        n_bands = len(self._band_meta)

        if sdr_type == 'flex':
            # FlexRadio DAX-IQ path — single-band only for now
            _, center_hz, _ = self._band_meta[0]
            flex_port = self.cfg.get('flex_udp_port', 7791)
            self.receiver = FlexIQReceiver(device_ip, freq_hz=int(center_hz),
                                           sample_rate=rx_sample_rate,
                                           udp_port=flex_port,
                                           control_port=sdr_port)
            log.info("Using FlexRadio DAX-IQ receiver at %s", device_ip)
        else:
            # HPSDR Protocol 1 path (Red Pitaya) — supports n_receivers for multi-band
            listen_port = self.cfg.get('hpsdr_listen_port', sdr_port)
            passive = self.cfg.get('passive', False)
            rx_filter = self.cfg.get('rx_filter', None)
            n_rx = max(self.cfg.get('max_receivers', 1), n_bands)
            lna_gain = self.cfg.get('lna_gain', 20)

            # Use C receiver for multi-band (handles 9600+ pkt/s)
            self._use_c_receiver = (n_bands > 1 and _get_hpsdr_fast() is not None)
            if self._use_c_receiver:
                self.receiver = _CReceiver(device_ip, sdr_port,
                                            n_rx, rx_sample_rate, lna_gain)
            else:
                self.receiver = HPSDRReceiver(device_ip, port=sdr_port,
                                              n_receivers=n_rx, sample_rate=rx_sample_rate,
                                              listen_port=listen_port,
                                              passive=passive, rx_filter=rx_filter)
                self.receiver.lna_gain = lna_gain
            for _name, center_hz, rx_idx in self._band_meta:
                self.receiver.set_frequency(rx_idx, center_hz)

        # ----------------------------------------------------------------
        # One InstanceManager per band
        # ----------------------------------------------------------------
        speeds_cfg = self.cfg.get('decoder_speeds', [0, 25, 30, 35])
        # Per-band PFB enable.  Two ways to specify:
        #   "use_pfb_scanner": true                — all bands on PFB
        #   "pfb_scanner_bands": ["20m", 7090000]  — listed bands on PFB (A/B test)
        # The list accepts band names ("20m") or center Hz ints (7090000).
        pfb_global = bool(self.cfg.get('use_pfb_scanner', False))
        pfb_bands_raw = self.cfg.get('pfb_scanner_bands', [])
        pfb_band_names = {str(b) for b in pfb_bands_raw}
        pfb_band_hz    = {int(b)  for b in pfb_bands_raw if isinstance(b, int)}
        # Per-band signal_min_snr override for PFB-using bands.  PFB has fixed
        # channelization cost regardless of bin count — we can run lower SNR
        # without the per-RX worker drowning the way per-bin scanners do.
        # Falls back to the global signal_min_snr when not set.
        pfb_min_snr = self.cfg.get('pfb_min_snr')
        global_min_snr = float(self.cfg.get('signal_min_snr', 8.0))
        self.managers = []
        for _name, _center_hz, _rx_idx in self._band_meta:
            use_pfb_here = (pfb_global
                            or _name in pfb_band_names
                            or _center_hz in pfb_band_hz)
            min_snr_here = (float(pfb_min_snr) if (use_pfb_here and pfb_min_snr is not None)
                            else global_min_snr)
            mgr = InstanceManager(
                sample_rate=rx_sample_rate,
                decoder_bin=self.cfg.get('decoder_bin', './uhsdr_cw'),
                max_instances=self.cfg.get('max_instances', 150),
                max_channels=self.cfg.get('max_channels', None),
                signal_timeout=self.cfg.get('signal_timeout', 90),
                speeds=speeds_cfg,
                bmorse_bin=self.cfg.get('bmorse_bin', None),
                hamfist_bin=self.cfg.get('hamfist_bin', None),
                hamfist_scp=self.cfg.get('hamfist_scp', None),
                ml_model_path=self.cfg.get('ml_model', None),
                ml_min_confidence=float(self.cfg.get('ml_min_confidence', 0.7)),
                use_dispatcher=bool(self.cfg.get('use_cpp_dispatcher', False)),
                use_pfb_dispatcher=bool(self.cfg.get('use_cpp_pfb', False)),
                use_itila=bool(self.cfg.get('use_itila', False)),
                itila_ev_thresh=float(self.cfg.get('itila_ev_thresh', 2.0)),
                itila_window_sec=float(self.cfg.get('itila_window_sec', 120.0)),
                itila_min_snr=min_snr_here,
                itila_max_bins=int(self.cfg.get('itila_max_bins', 200)),
                use_pfb_scanner=use_pfb_here,
                valid_calls=calls,
                cw_min_khz=float(self.cfg.get('cw_min_khz', 0)),
                cw_max_khz=float(self.cfg.get('cw_max_khz', 99999)),
                enable_caller_spotting=bool(self.cfg.get('enable_caller_spotting', True)),
            )
            self.managers.append(mgr)
        # Legacy single-manager ref
        self.manager = self.managers[0]

        self.receiver.start()
        self.running = True

        for name, center_hz, rx_idx in self._band_meta:
            cal_center = center_hz * 0.9999961
            log.info("SparkGap LIVE: %s (%.3f kHz) rx%d, telnet :%d",
                     name, cal_center / 1000, rx_idx,
                     self.cfg.get('telnet_port', 7300))

        # SIGUSR1 → snapshot the current IQ buffer for every enabled band
        # to /tmp/diag_<band>_<HHMMSS>.wav for live-vs-replay diagnostics.
        # Reuses the FT8 capture buffer (must have enable_ft8=true).
        if (getattr(self, '_use_c_receiver', False)
                and self.cfg.get('enable_ft8', True)):
            try:
                signal.signal(signal.SIGUSR1, self._diag_iq_snapshot)
                log.info("SIGUSR1 handler installed: kill -USR1 %d for IQ snapshot", os.getpid())
            except Exception as e:
                log.warning("Failed to install SIGUSR1 handler: %s", e)

        return True

    def _diag_iq_snapshot(self, signum, frame):
        """SIGUSR1 handler — non-destructive snapshot of FT8 capture buffer
        for every band → /tmp/diag_<band-khz>_<HHMMSS>.wav (24-bit stereo
        IQ at 192 kHz). Replay with:
          sparkgap.py --file <wav> --center-khz <khz> --start-min 0 --end-min 1
        FT8 buffer stores int24 values cast to float (range ±8388608) —
        we write them straight back as 24-bit PCM so the file reader gets
        the full Pitaya range. read_24bit_iq_chunk reads exactly this."""
        try:
            import ctypes as _ct
            from datetime import datetime as _dt
            ts = _dt.now().strftime('%H%M%S')
            n_max = 192000 * 65  # FT8_BUF_CAP
            i_buf = (_ct.c_float * n_max)()
            q_buf = (_ct.c_float * n_max)()
            t_first = _ct.c_double(0)
            for name, center_hz, rx_idx in self._band_meta:
                n = self.receiver.lib.hpsdr_iq_snapshot_read(
                    self.receiver._h, rx_idx, i_buf, q_buf, n_max, _ct.byref(t_first))
                if n < 192000:
                    log.warning("SIGUSR1 rx%d (%s): only %d samples — skipping",
                                rx_idx, name, n)
                    continue
                khz = int(center_hz / 1000)
                path = f"/tmp/diag_{khz}_{ts}.wav"
                i_arr = np.frombuffer(i_buf, dtype=np.float32, count=n)
                q_arr = np.frombuffer(q_buf, dtype=np.float32, count=n)
                # Clip to int24 range, convert to int32 then pack as 3-byte LE
                i32 = np.clip(i_arr, -8388608, 8388607).astype(np.int32)
                q32 = np.clip(q_arr, -8388608, 8388607).astype(np.int32)
                interleaved = np.empty(n * 2, dtype=np.int32)
                interleaved[0::2] = i32
                interleaved[1::2] = q32
                # Take low 3 bytes of each int32 (little-endian)
                raw_bytes = interleaved.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
                # Write 24-bit PCM WAV manually (Python wave module won't)
                rate = 192000
                channels = 2
                byte_rate = rate * channels * 3
                block_align = channels * 3
                data_size = len(raw_bytes)
                fmt_chunk = struct.pack('<HHIIHH', 1, channels, rate,
                                        byte_rate, block_align, 24)
                with open(path, 'wb') as f:
                    f.write(b'RIFF')
                    f.write(struct.pack('<I', 36 + data_size))
                    f.write(b'WAVE')
                    f.write(b'fmt ')
                    f.write(struct.pack('<I', len(fmt_chunk)))
                    f.write(fmt_chunk)
                    f.write(b'data')
                    f.write(struct.pack('<I', data_size))
                    f.write(raw_bytes)
                log.info("SIGUSR1: wrote %s (%d samples, %.1f sec, t_first=%.3f)",
                         path, n, n / 192000.0, t_first.value)
        except Exception as e:
            log.error("SIGUSR1 snapshot failed: %s", e)

    async def stop(self):
        self.running = False
        if self.receiver:
            if getattr(self, '_use_c_receiver', False):
                self.receiver.stop()
                self.receiver.destroy()
            else:
                self.receiver.close()
        for mgr in self.managers:
            mgr.kill_all()
        if self.telnet:
            await self.telnet.stop()
        if self._wav_record:
            self._wav_record.close()
            self._wav_record = None
        elapsed = time.time() - self.start_time if self.start_time else 0
        log.info("Stopped: %d spots in %.0fs", self.spot_count, elapsed)

    def _iq_callback(self, rx_index, iq_samples):
        """Called from HPSDR receiver thread — buffer only, no processing.

        Routes IQ data to the per-band buffer for rx_index.  Unknown rx_index
        values are silently dropped (e.g. extra receivers not in config).
        """
        try:
            buf = self._band_bufs.get(rx_index)
            if buf is None:
                return
            # Minimal GIL work: just stash the raw sample list.
            # Numpy conversion and deque update happen in the async loop.
            with self._iq_lock:
                buf['raw'].append(iq_samples)
            if self._wav_record and rx_index == 0:
                import struct as _struct
                frames = bytearray()
                for i_val, q_val in iq_samples:
                    i16 = max(-32768, min(32767, int(i_val * 32767)))
                    q16 = max(-32768, min(32767, int(q_val * 32767)))
                    frames += _struct.pack('<hh', i16, q16)
                self._wav_record.writeframes(bytes(frames))
        except Exception:
            pass  # Don't let errors kill the receiver thread

    async def run(self):
        use_c = getattr(self, '_use_c_receiver', False)
        if use_c:
            self.receiver.start()
        else:
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
            live_rate = self.cfg.get('sample_rate', 48000)
            # ----------------------------------------------------------------
            # Per-band: drain IQ and push to decoders
            # ----------------------------------------------------------------
            for (band_name, center_hz, rx_idx), mgr in zip(self._band_meta, self.managers):
                if use_c:
                    # C worker thread handles drain → scanner feed
                    # Just ensure scanners are created and registered
                    if mgr._itila_scanner is None and mgr.use_itila:
                        center_khz = center_hz / 1000
                        mgr.update_signals([], center_khz)
                    scanner = mgr._itila_scanner
                    if scanner and scanner._sc and not getattr(self, '_worker_started', False):
                        import ctypes as _ct
                        feed_ptr = _ct.cast(scanner._sc._lib.itila_sc_feed_iq, _ct.c_void_p)
                        self.receiver.lib.hpsdr_set_scanner(
                            self.receiver._h, rx_idx, scanner._sc._h,
                            feed_ptr, _ct.c_double(8388608.0))
            # Start worker thread once all scanners are registered
            if use_c and not getattr(self, '_worker_started', False):
                scanners_ready = sum(1 for mgr in self.managers
                                     if mgr._itila_scanner and mgr._itila_scanner._sc)
                if scanners_ready == len(self.managers):
                    import ctypes as _ct
                    # Per-RX decode setup.  Each scanner registers its native
                    # decode entry point and envelope-sample window.  For PFB
                    # scanners the lib shim transparently routes the
                    # itila_sc_decode_ready name to pfb_sc_decode_ready —
                    # both produce the byte-identical ScDecodeResult struct,
                    # so the C worker writes them into the same result buffer
                    # and Python's poll loop routes them the same way.
                    have_per_rx = hasattr(self.receiver.lib, 'hpsdr_set_rx_decode')
                    for (bn, ch, ri), mgr in zip(self._band_meta, self.managers):
                        scanner = mgr._itila_scanner
                        if not (scanner and scanner._sc):
                            continue
                        decode_ptr = _ct.cast(
                            scanner._sc._lib.itila_sc_decode_ready,
                            _ct.c_void_p)
                        window_samples = scanner._window_samples
                        if have_per_rx:
                            self.receiver.lib.hpsdr_set_rx_decode(
                                self.receiver._h, ri,
                                decode_ptr, _ct.c_int(window_samples))
                        else:
                            # Old hpsdr_fast — single decode setter.
                            # Safe only when all bands use the same backend.
                            self.receiver.lib.hpsdr_set_decode(
                                self.receiver._h, decode_ptr,
                                _ct.c_int(window_samples))
                    self._pfb_managers = []  # no Python decode poll needed
                    # Enable FT8 raw IQ accumulation per band (skipped if disabled)
                    if self.cfg.get('enable_ft8', True):
                        FT8_FREQS = {3590: 3573, 7090: 7074, 14090: 14074,
                                     21090: 21074, 28090: 28074}
                        for (bn, ch, ri), mgr in zip(self._band_meta, self.managers):
                            ck = ch / 1000
                            ft8_khz = FT8_FREQS.get(int(ck))
                            if ft8_khz:
                                self.receiver.lib.hpsdr_enable_ft8(
                                    self.receiver._h, ri,
                                    _ct.c_double(ft8_khz * 1000.0),
                                    _ct.c_double(float(ch)))
                                log.info("FT8 accumulator enabled rx%d: %.0f kHz", ri, ft8_khz)
                    else:
                        log.info("FT8 disabled by config (enable_ft8=false)")
                    self.receiver.lib.hpsdr_start_worker(self.receiver._h)
                    self._worker_started = True
                    log.info("C worker thread started (%d bands, CW+FT8)",
                             scanners_ready)
            # PFB scanners' C worker only feeds; decode runs in Python.
            # Drain ready windows and route through the scanner's normal
            # _process_ready (Python decode + spotting path).
            if use_c and getattr(self, '_worker_started', False):
                for mgr in getattr(self, '_pfb_managers', []):
                    sc = mgr._itila_scanner
                    if sc:
                        sc._process_ready()

            # Process decode results — poll from C worker's result buffer
            if use_c and getattr(self, '_worker_started', False):
                import ctypes as _ct
                result_size = 280
                max_poll = 128
                if not hasattr(self, '_poll_buf'):
                    self._poll_buf = _ct.create_string_buffer(result_size * max_poll)
                poll_buf = self._poll_buf
                n = self.receiver.lib.hpsdr_poll_results(
                    self.receiver._h, poll_buf, _ct.c_int(max_poll))
                now = time.time()
                for i in range(n):
                    off = i * result_size
                    f_hz = _ct.c_double.from_buffer(poll_buf, off).value
                    snr = _ct.c_double.from_buffer(poll_buf, off + 8).value
                    wpm = _ct.c_int.from_buffer(poll_buf, off + 16).value
                    raw = poll_buf[off+24:off+result_size].split(b'\0')[0].decode('ascii', errors='replace')
                    if not raw:
                        continue
                    f_khz = f_hz / 1000.0
                    log.info("ITILA raw %.1f kHz: %r", f_khz, raw[:80])
                    # Find or create bin state for ticker tape
                    scanner = None
                    for mgr in self.managers:
                        if mgr._itila_scanner:
                            scanner = mgr._itila_scanner
                            break
                    if not scanner:
                        continue
                    if f_hz not in scanner._bins:
                        scanner._bins[f_hz] = {
                            'h100': None, 'h200': None, 'pending': [],
                            'wpm': 0, 'snr': 0.0, 'text_buf': '',
                            'spotted': set(), 'last_cq_time': 0.0,
                        }
                    st = scanner._bins[f_hz]
                    st['snr'] = snr
                    if wpm > 0:
                        st['wpm'] = wpm
                    st['text_buf'] = (st['text_buf'] + ' ' + raw)[-512:]
                    if CQ_PATTERNS.search(raw):
                        st['last_cq_time'] = now
                    call = _itila_extract_cq_call(raw, self.tracker.valid_calls)
                    if call and call not in st['spotted']:
                        st['spotted'].add(call)
                        st['pending'].append(f'CQ {call} ')
                        log.info("ITILA scan %.1f kHz: %s %d WPM (raw: %s)",
                                 f_khz, call, wpm, raw[:60])
                    elif now - st['last_cq_time'] < 120.0:
                        # runner-only context extraction (see _process_ready)
                        call = _itila_extract_cq_call(st['text_buf'], self.tracker.valid_calls)
                        if call and call not in st['spotted']:
                            st['spotted'].add(call)
                            st['pending'].append(f'CQ {call} ')
                            log.info("ITILA context %.1f kHz: %s %d WPM",
                                     f_khz, call, wpm)
                else:
                    # Python receiver path: drain from callback buffers
                    with self._iq_lock:
                        buf = self._band_bufs[rx_idx]
                        raw_chunks = buf['raw']
                        buf['raw'] = []
                    if raw_chunks:
                        chunk_size = len(raw_chunks[0])
                        max_chunks = max(1, live_rate // 5 // chunk_size)
                        if len(raw_chunks) > max_chunks:
                            raw_chunks = raw_chunks[-max_chunks:]
                        feed_i = []
                        feed_q = []
                        for chunk in raw_chunks:
                            iq_arr = np.asarray(chunk, dtype=np.float64)
                            feed_i.append(iq_arr[:, 0] * 8388608.0)
                            feed_q.append(iq_arr[:, 1] * 8388608.0)
                        max_iq_buf = live_rate * 10
                        iq_deq = buf['iq']
                        for chunk in raw_chunks:
                            iq_deq.extend(chunk)
                        while len(iq_deq) > max_iq_buf:
                            iq_deq.popleft()
                        i_cat = np.concatenate(feed_i)
                        q_cat = np.concatenate(feed_q)
                        mgr.feed_all_iq(i_cat, q_cat)

            # ----------------------------------------------------------------
            # Periodic signal scan — one FFT per band
            # ----------------------------------------------------------------
            if now - last_scan >= scan_interval:
                last_scan = now
                self.tracker.reset_cycle()

                # Hot-reload add_calls.txt if file changed since last scan
                if os.path.exists(self._add_calls_path):
                    mtime = os.path.getmtime(self._add_calls_path)
                    if mtime != self._add_calls_mtime:
                        self._add_calls_mtime = mtime
                        new_add = set()
                        with open(self._add_calls_path) as f:
                            for line in f:
                                line = line.strip().upper()
                                if line and not line.startswith('#'):
                                    new_add.add(line)
                        added = new_add - self.tracker.add_calls
                        removed = self.tracker.add_calls - new_add
                        self.tracker.add_calls = new_add
                        self.tracker.valid_calls |= new_add
                        for call in added:
                            self.tracker._scp_by_len[len(call)].append(call)
                        if added or removed:
                            log.info("add_calls reloaded: +%d -%d calls", len(added), len(removed))

                # In C receiver mode, scanner has its own FFT scan — skip Python FFT
                if not use_c:
                    fft_size = 8192
                    min_snr = self.cfg.get('signal_min_snr', 12)
                    cw_min  = self.cfg.get('cw_min_khz', 0)
                    cw_max  = self.cfg.get('cw_max_khz', 99999)

                    for (band_name, center_hz, rx_idx), mgr in zip(self._band_meta, self.managers):
                        center_khz = center_hz / 1000
                        with self._iq_lock:
                            iq_deque = self._band_bufs[rx_idx]['iq']
                            if len(iq_deque) >= fft_size:
                                raw = list(itertools.islice(iq_deque,
                                                            len(iq_deque) - fft_size,
                                                            None))
                            else:
                                raw = None

                        if raw is None:
                            continue

                        iq_arr = np.array([complex(i, q) for i, q in raw])
                        window  = np.blackman(fft_size)
                        psd     = np.abs(np.fft.fft(iq_arr * window)) ** 2
                        psd_db  = 10 * np.log10(psd + 1e-20)
                        noise   = np.median(psd_db)

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

                        clustered = []
                        for freq, snr in sorted(signals):
                            if not clustered or abs(freq - clustered[-1][0]) > 100:
                                clustered.append((freq, snr))
                            elif snr > clustered[-1][1]:
                                clustered[-1] = (freq, snr)

                        log.info("[%s] noise=%.1f dB  thresh=%.1f dB  buf=%d samples",
                                 band_name, noise, noise + min_snr, len(iq_deque))
                        top = sorted(signals, key=lambda x: -x[1])[:10]
                        for f, s in top:
                            log.info("  [%s] PEAK %.3f kHz %.1f dB",
                                     band_name, center_khz + f/1000, s)
                        if not top:
                            log.info("  [%s] (no peaks above threshold)", band_name)

                        if cw_min or cw_max < 99999:
                            clustered = [(f, s) for f, s in clustered
                                         if cw_min <= center_khz + f/1000 <= cw_max]
                        mgr.update_signals(clustered, center_khz)

            # ----------------------------------------------------------------
            # Collect decoder output from every band — spots to shared telnet
            # ----------------------------------------------------------------
            max_spot_wpm = self.cfg.get('max_spot_wpm', 0)  # 0 = unlimited
            for (_band_name, _center_hz, _rx_idx), mgr in zip(self._band_meta, self.managers):
                results = mgr.collect_all()
                for rf_khz, snr, text, ctx, dec_id, dec_type, wpm in results:
                    log.debug("DECODED %.1f kHz: %r", rf_khz, text[:120])
                    alnum = re.sub(r'[^A-Z0-9]', '', (ctx or text).upper())
                    if len(alnum) < 4:
                        continue
                    if max_spot_wpm and wpm > max_spot_wpm:
                        log.debug("WPM cap: %.1f kHz %d WPM > %d, skipped",
                                  rf_khz, wpm, max_spot_wpm)
                        continue
                    spots = self.tracker.process(rf_khz, snr, text, ctx, dec_id,
                                                 dec_type=dec_type, wpm=wpm)
                    for spot in spots:
                        self.spot_count += 1
                        self.telnet.broadcast_spot(
                            freq_khz=spot['freq_khz'],
                            dx_call=spot['call'],
                            snr=spot['snr'],
                            wpm=spot.get('wpm', 0),
                        )
                        method   = spot.get('method', 'exact')
                        spot_wpm = spot.get('wpm', 0)
                        log.info("*** SPOT: %10.1f  %-12s  %d dB  %d WPM  [%s] ***",
                                 spot['freq_khz'], spot['call'], spot['snr'],
                                 spot_wpm, method)
                        if self._spot_log:
                            import datetime
                            ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                            self._spot_log.write(
                                f"{ts} UTC | {spot['call']} | {spot['freq_khz']:.1f} | "
                                f"{spot['snr']:.0f} | {spot_wpm} WPM\n"
                            )

            # ----------------------------------------------------------------
            # Status — aggregate across all bands
            # ----------------------------------------------------------------
            if now - last_status >= status_interval:
                last_status = now
                elapsed = now - self.start_time
                total_decoders = sum(mgr.count for mgr in self.managers)
                total_chars    = sum(
                    g.total_chars
                    for mgr in self.managers
                    for g in mgr.instances.values()
                )
                log.info("Status: %d spots, %d decoders (%d bands), %d clients, "
                         "%d chars, %.0fs",
                         self.spot_count, total_decoders, len(self.managers),
                         self.telnet.client_count, total_chars, elapsed)
                # Ring + env-cap drop telemetry (added 2026-04-26 to verify
                # we're not silently losing samples in the C pipeline).
                if use_c and self.receiver and self.receiver._h:
                    try:
                        rl = self.receiver.lib
                        pkts  = rl.hpsdr_pkt_count(self.receiver._h)
                        rdrop = rl.hpsdr_drop_count(self.receiver._h)
                        env_drops_total = 0
                        bins_total = 0
                        bins_peak = 0
                        for mgr in self.managers:
                            wrapper = getattr(mgr, '_itila_scanner', None)
                            sc = getattr(wrapper, '_sc', None) if wrapper else None
                            if sc and sc._h:
                                if hasattr(sc._lib, 'itila_sc_env_drops'):
                                    sc._lib.itila_sc_env_drops.restype = _ct.c_ulonglong
                                    sc._lib.itila_sc_env_drops.argtypes = [_ct.c_void_p]
                                    sc._lib.itila_sc_bins_peak.restype = _ct.c_int
                                    sc._lib.itila_sc_bins_peak.argtypes = [_ct.c_void_p]
                                    sc._lib.itila_sc_bin_count.restype = _ct.c_int
                                    sc._lib.itila_sc_bin_count.argtypes = [_ct.c_void_p]
                                    env_drops_total += sc._lib.itila_sc_env_drops(sc._h)
                                    bins_peak = max(bins_peak, sc._lib.itila_sc_bins_peak(sc._h))
                                    bins_total += sc._lib.itila_sc_bin_count(sc._h)
                        log.info("Health: ring_drops=%d/%d (%.4f%%) env_drops=%d bins=%d peak=%d",
                                 rdrop, pkts, 100.0 * rdrop / max(pkts + rdrop, 1),
                                 env_drops_total, bins_total, bins_peak)
                    except Exception as e:
                        log.warning("Health probe failed: %s", e)

            # FT8 decode — aligned to minute boundaries, runs in thread
            if use_c and getattr(self, '_worker_started', False) \
                    and self.cfg.get('enable_ft8', True):
                ft8_now = time.time()
                ft8_sec = ft8_now % 60
                ft8_prev_sec = getattr(self, '_ft8_prev_sec', 60)
                if ft8_sec < 5 and ft8_prev_sec >= 55 and \
                        not getattr(self, '_ft8_running', False):
                    self._ft8_running = True
                    import ctypes as _ct, struct
                    FT8_FREQS = {3590: 3573, 7090: 7074, 14090: 14074,
                                 21090: 21074, 28090: 28074}
                    ft8_jobs = []
                    n60 = 192000 * 60
                    buf_cap = 192000 * 65
                    pkt_lost = 0
                    if hasattr(self.receiver.lib, 'hpsdr_pkt_lost'):
                        try:
                            pkt_lost = self.receiver.lib.hpsdr_pkt_lost(self.receiver._h)
                        except Exception:
                            pass
                    pkts = self.receiver.lib.hpsdr_pkt_count(self.receiver._h)
                    log.info("FT8 trigger: sec=%.2f pkts=%d lost=%d (%.2f%%)",
                             ft8_sec, pkts, pkt_lost,
                             100.0*pkt_lost/max(pkts+pkt_lost, 1))
                    # Snapshot all bands first (one swap each — fast, atomic).
                    # Each snapshot is the previous minute's data filling the
                    # active buffer between the prior swap and this one.
                    for (bn, ch, ri), mgr in zip(self._band_meta, self.managers):
                        ck = ch / 1000
                        ft8_khz = FT8_FREQS.get(int(ck))
                        if not ft8_khz:
                            continue
                        fi = np.empty(buf_cap, dtype=np.float32)
                        fq = np.empty(buf_cap, dtype=np.float32)
                        t_first = _ct.c_double(0.0)
                        n = self.receiver.lib.hpsdr_ft8_swap_read(
                            self.receiver._h, ri,
                            fi.ctypes.data_as(_ct.POINTER(_ct.c_float)),
                            fq.ctypes.data_as(_ct.POINTER(_ct.c_float)),
                            buf_cap, _ct.byref(t_first))
                        log.info("FT8 %s rx%d: swap n=%d (%.2f s) t_first=%.3f",
                                 bn, ri, n, n / 192000.0, t_first.value)
                        # Allow up to 1 sec slack — swap-to-swap timing can
                        # land the snapshot just under 60 sec depending on
                        # where Python triggers within the second.
                        if n < n60 - 192000:
                            log.info("FT8 %s rx%d: short snapshot, skip", bn, ri)
                            continue
                        # Take the last min(n, n60) samples — the active buffer
                        # was reset by the prior swap so this is automatically
                        # minute-aligned (within ~50 ms).
                        take = min(n, n60)
                        fi_60 = fi[n - take:n].copy()
                        fq_60 = fq[n - take:n].copy()
                        ft8_jobs.append((bn, ri, ck, ft8_khz,
                                         fi_60, fq_60, take))
                    if ft8_jobs:
                        def _ft8_decode(jobs, skimmer):
                            import subprocess, wave
                            from scipy.signal import resample_poly
                            from numpy.fft import fft, ifft
                            ft8_bin = '/home/sparkgap/decode_ft8'
                            total = 0
                            seen_msgs = set()  # dedupe across sliding windows
                            for bn, ri, ck, ft8_khz, fi, fq, n in jobs:
                                iq = fi.astype(np.float64) + 1j * fq.astype(np.float64)
                                offset_hz = (ft8_khz - ck) * 1000
                                t = np.arange(n) / 192000
                                mixed = iq * np.exp(-1j * 2 * np.pi * offset_hz * t)
                                # 192k → 12k complex via polyphase
                                dec12 = resample_poly(mixed, 1, 16)
                                slot_n = 15 * 12000
                                # SLIDING window — slot start every 1 second.
                                # Fixed sub-periods miss decodes due to FT8 transmission
                                # offset (~0.5s past minute boundary) — we sweep 0–45s
                                # in 1s steps to align with whatever the actual TX boundary is.
                                for start in range(0, len(dec12) - slot_n + 1, 12000):
                                    chunk = dec12[start:start+slot_n]
                                    if len(chunk) < slot_n:
                                        continue
                                    # USB demodulation: zero negative freqs, take real
                                    spec = fft(chunk)
                                    spec[len(spec)//2:] = 0
                                    audio = ifft(spec).real * 2
                                    peak = np.max(np.abs(audio))
                                    if peak > 0:
                                        audio = audio / peak * 0.9
                                    i16 = (audio * 32767).astype(np.int16)
                                    part = start // 12000  # used in WAV filename
                                    wav_fn = f'/tmp/ft8_{bn}_p{part}.wav'
                                    with wave.open(wav_fn, 'wb') as wf:
                                        wf.setnchannels(1); wf.setsampwidth(2)
                                        wf.setframerate(12000)
                                        wf.writeframes(i16.tobytes())
                                    try:
                                        r = subprocess.run([ft8_bin, wav_fn],
                                            capture_output=True, text=True, timeout=20)
                                    except Exception as e:
                                        log.debug("decode_ft8 err %s p%d: %s", bn, part, e)
                                        continue
                                    for line in (r.stdout or '').strip().split('\n'):
                                        line = line.strip()
                                        if not line:
                                            continue
                                        # Format: "000000 +05.0 +1.12 1803 ~  CQ N4DWD EM86"
                                        parts_o = line.split(maxsplit=5)
                                        if len(parts_o) < 6:
                                            continue
                                        try:
                                            snr = int(float(parts_o[1]))
                                            audio_hz = int(parts_o[3])
                                            msg = parts_o[5]
                                        except (ValueError, IndexError):
                                            continue
                                        # RF freq = dial + audio offset
                                        rf_khz = ft8_khz + audio_hz / 1000.0
                                        # Extract caller from message
                                        # CQ X Y → X is calling
                                        # X Y Z → Y is responding to X (spot Y)
                                        # X Y RR73/grid → spot Y
                                        msg_parts = msg.split()
                                        ft8_call = None
                                        if msg_parts and msg_parts[0] == 'CQ':
                                            # CQ [DX] CALL GRID — call is at index 1 or 2
                                            if len(msg_parts) >= 2 and re.match(
                                                    r'^[A-Z0-9]{1,3}[0-9][A-Z]{1,4}$',
                                                    msg_parts[1]):
                                                ft8_call = msg_parts[1]
                                            elif len(msg_parts) >= 3:
                                                ft8_call = msg_parts[2]
                                        elif len(msg_parts) >= 2:
                                            # Standard QSO: X Y ... — Y is the spotted station
                                            ft8_call = msg_parts[1]
                                        if not ft8_call or not re.match(
                                                r'^[A-Z0-9/]{3,11}$', ft8_call):
                                            continue
                                        # Dedupe: same band + msg = duplicate from
                                        # sliding window catching same TX twice
                                        dedup_key = (bn, msg)
                                        if dedup_key in seen_msgs:
                                            continue
                                        seen_msgs.add(dedup_key)
                                        log.info("FT8 %s p%d: %s", bn, part, line)
                                        total += 1
                                        skimmer.spot_count += 1
                                        skimmer.telnet.broadcast_spot(
                                            freq_khz=rf_khz, dx_call=ft8_call,
                                            snr=snr, mode='FT8',
                                            comment=msg)
                                        log.info("*** SPOT: %10.1f  %-12s  %+d dB  FT8  [%s] ***",
                                                 rf_khz, ft8_call, snr, msg)
                            log.info("FT8 cycle done: %d spots across %d bands",
                                     total, len(jobs))
                            # RTTY scan piggybacks on the same minute snapshot.
                            # Run after FT8 so the FT8 spots flow first.
                            rtty_lib = _get_rtty_lib()
                            if rtty_lib is not None:
                                rtty_total = 0
                                for bn, ri, ck, ft8_khz, fi, fq, n in jobs:
                                    try:
                                        rtty_total += _rtty_scan_band(
                                            skimmer, rtty_lib, bn, ck, fi, fq, n)
                                    except Exception as e:
                                        log.warning("RTTY scan %s: %s", bn, e)
                                log.info("RTTY cycle done: %d spots across %d bands",
                                         rtty_total, len(jobs))
                            skimmer._ft8_running = False
                        import threading
                        threading.Thread(target=_ft8_decode,
                                         args=(ft8_jobs, self),
                                         daemon=True).start()
                    else:
                        self._ft8_running = False
                self._ft8_prev_sec = ft8_sec

            await asyncio.sleep(0.1 if use_c else 0.025)


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
    skimmer = SparkGap(config)

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

    calls, blacklist, add_calls = load_callsign_db(
        config.get('master_scp', 'MASTER.SCP'),
        config.get('add_calls', 'add_calls.txt'),
        config.get('blacklist', 'blacklist.txt'),
    )
    gate_config = {k: config[k] for k in SpotTracker.GATE_DEFAULTS if k in config}
    tracker = SpotTracker(calls, blacklist, respot_interval=0, add_calls=add_calls,
                          scp_bypass_threshold=int(config.get('scp_bypass_threshold', 0)),
                          gate_config=gate_config)

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
        max_channels=config.get('max_channels', None),
        signal_timeout=9999,  # don't kill during file processing
        speeds=speeds,
        bmorse_bin=config.get('bmorse_bin'),
        ml_model_path=config.get('ml_model'),
        ml_min_confidence=float(config.get('ml_min_confidence', 0.7)),
        use_dispatcher=bool(config.get('use_cpp_dispatcher', False)),
        use_pfb_dispatcher=bool(config.get('use_cpp_pfb', False)),
        use_itila=bool(config.get('use_itila', False)),
        itila_ev_thresh=float(config.get('itila_ev_thresh', 2.0)),
        itila_window_sec=float(config.get('itila_window_sec', 120.0)),
        # Match live mode: use config values for itila_min_snr / max_bins /
        # use_pfb_scanner instead of letting the InstanceManager defaults
        # (8.0, 200, False) silently apply. Pre-fix divergence documented
        # in feedback_file_vs_live_config_divergence.md.
        itila_min_snr=float(config.get('signal_min_snr', 12)),
        itila_max_bins=int(config.get('itila_max_bins', 200)),
        use_pfb_scanner=bool(config.get('use_pfb_scanner', False)),
        valid_calls=calls,  # required for the SCP-bias path in extractor
        cw_min_khz=float(config.get('cw_min_khz', 0)),
        cw_max_khz=float(config.get('cw_max_khz', 99999)),
        enable_caller_spotting=bool(config.get('enable_caller_spotting', True)),
    )

    center_khz = args.center_khz
    cw_min = float(config.get('cw_min_khz', 0))
    cw_max = float(config.get('cw_max_khz', 99999))
    if cw_min > 0 and (center_khz < cw_min - 100 or center_khz > cw_max + 100):
        log.warning("Center freq %.0f kHz is outside config band limits %.0f-%.0f kHz — "
                    "overriding to center ±100 kHz", center_khz, cw_min, cw_max)
        cw_min = center_khz - 100
        cw_max = center_khz - 20
        config['cw_min_khz'] = cw_min
        config['cw_max_khz'] = cw_max
        manager.cw_min_khz = cw_min
        manager.cw_max_khz = cw_max
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

    min_snr = config.get('signal_min_snr', 8)

    # Always use high-resolution FFT for signal detection (23 Hz bins vs PFB 250 Hz).
    # PFB has 10 dB less per-bin SNR than FFT, so it misses weak stations.
    # PFB is used only for channelization, not detection.
    fft_size = 8192

    def _scan_iq(i_arr, q_arr):
        """Run FFT-average signal detection on an IQ slice. Returns clustered list."""
        n_ffts_local = min(len(i_arr) // fft_size, 200)
        if n_ffts_local == 0:
            return []
        avg = np.zeros(fft_size)
        for fi in range(n_ffts_local):
            ch = i_arr[fi*fft_size:(fi+1)*fft_size] + \
                 1j * q_arr[fi*fft_size:(fi+1)*fft_size]
            avg += np.abs(np.fft.fft(ch * np.hanning(fft_size))) ** 2
        avg /= n_ffts_local
        avg_db_l = 10 * np.log10(avg + 1e-20)
        freqs_l = np.fft.fftfreq(fft_size, 1.0 / file_rate)
        noise_l = np.median(avg_db_l)
        sigs = []
        for i in range(1, fft_size - 1):
            if avg_db_l[i] > noise_l + min_snr and \
               avg_db_l[i] > avg_db_l[i-1] and avg_db_l[i] > avg_db_l[i+1]:
                sigs.append((freqs_l[i], avg_db_l[i] - noise_l))
        cl = []
        for freq, snr in sorted(sigs):
            if not cl or abs(freq - cl[-1][0]) > 200:
                cl.append((freq, snr))
            elif snr > cl[-1][1]:
                cl[-1] = (freq, snr)
        return cl

    clustered = _scan_iq(scan_i, scan_q)
    log.info("  FFT: %d signals detected", len(clustered))
    if manager._pfb is not None:
        # Reset PFB state — scan audio was used only for FFT detection above
        manager._pfb._hist[:] = 0
        manager._pfb._phase_vec[:] = 0
        manager._pfb._buf = np.zeros(0, dtype=np.complex128)
        manager._pfb.last_output = None

    manager.update_signals(clustered, center_khz)
    del scan_i, scan_q

    # Feed continuous audio chunks — same decoder instances for entire recording
    for t_start in np.arange(start_sec, end_sec, chunk_sec):
        t_end = min(t_start + chunk_sec, end_sec)
        dur = t_end - t_start
        # Reset hallucination filter each chunk — mirrors live-mode scan interval.
        # Without this, _cycle_calls accumulates across the entire recording: a call
        # appearing at 3+ nearby channels (due to signal bleed) gets permanently
        # blocked even though it's a real station. In live mode reset_cycle() fires
        # every scan_interval seconds; file mode must do the same.
        tracker.reset_cycle()
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
        # Periodic re-scan mirrors live-mode scan_interval. Without this the
        # file-mode scanner runs exactly once at t=0 on ~8.5 s of data, and
        # signals that key up later in the window (e.g. EB1EOE from t+405s)
        # are never discovered → never get a channel spawned for the entire
        # eval. update_signals() only adds channels, never removes them
        # (signal_timeout=9999 in file mode), so re-scanning is safe.
        rescan_interval_sec = int(config.get('rescan_interval_sec', 30))
        rescan_samples = int(rescan_interval_sec * file_rate)
        next_rescan_pos = rescan_samples
        scan_slice_samples = fft_size * 200  # ~8.5s matches initial scan
        pileup_interval_sec = 30
        pileup_samples = int(pileup_interval_sec * file_rate)
        next_pileup_pos = pileup_samples
        spotted_freqs = set()  # kHz values of confirmed spots — passed to detect_pileups filter 3
        for pos in range(0, len(i_data), block_size):
            if pos >= next_rescan_pos:
                s_start = max(0, pos - scan_slice_samples)
                s_end = min(len(i_data), pos)
                new_clustered = _scan_iq(i_data[s_start:s_end],
                                         q_data[s_start:s_end])
                prev_count = len(manager.instances)
                manager.update_signals(new_clustered, center_khz)
                added = len(manager.instances) - prev_count
                if added > 0:
                    log.info("  Re-scan t=%.0fs: +%d signals (%d total, %d peaks)",
                             t_start + pos/file_rate, added,
                             len(manager.instances), len(new_clustered))
                next_rescan_pos += rescan_samples
            if pos >= next_pileup_pos:
                t_now = t_start + pos / file_rate
                pileups = manager.detect_pileups(spotted_freqs=spotted_freqs, now=t_now)
                for pu in pileups:
                    members_str = ', '.join('%.1f' % f for f in pu['members'])
                    log.info("PILEUP t=%.0fs: floor=%.1f kHz  top=%.1f kHz  size=%d"
                             "  snr_max=%.0f dB  dx_tx=%.1f kHz  passes=%d"
                             "  callers=[%s]",
                             t_now, pu['floor_khz'], pu['top_khz'], pu['size'],
                             pu['snr_max'], pu['dx_tx_khz'], pu['passes'],
                             members_str)
                if not pileups:
                    log.debug("PILEUP t=%.0fs: no confirmed clusters", t_now)
                next_pileup_pos += pileup_samples
            i_block = i_data[pos:pos+block_size]
            q_block = q_data[pos:pos+block_size]
            manager.feed_all_iq(i_block, q_block)

            # Collect output periodically
            results = manager.collect_all()
            for rf_khz, snr, text, ctx, dec_id, dec_type, wpm in results:
                total_chars += len(text)
                total_results += 1
                spots = tracker.process(rf_khz, snr, text, ctx, dec_id,
                                        dec_type=dec_type, wpm=wpm)
                for spot in spots:
                    all_spots.append(spot)
                    spotted_freqs.add(spot['freq_khz'])
                    log.info("SPOT: %.1f kHz %s %d dB %d WPM [%s]",
                             spot['freq_khz'], spot['call'],
                             spot['snr'], spot.get('wpm', 0), spot['method'])

        # Final collect after all data fed — give decoders a moment to flush
        import time as _time
        _time.sleep(0.5)
        results = manager.collect_all()
        for rf_khz, snr, text, ctx, dec_id, dec_type, wpm in results:
            total_chars += len(text)
            total_results += 1
            spots = tracker.process(rf_khz, snr, text, ctx, dec_id,
                                    dec_type=dec_type, wpm=wpm)
            for spot in spots:
                all_spots.append(spot)

        unique_so_far = len({s['call'] for s in all_spots})
        log.info("  Chunk decoded: %d text outputs, %d total chars, %d spots (%d unique so far)",
                 total_results, total_chars, len(all_spots), unique_so_far)
        # Flush stdout so file-mode progress is visible without waiting for exit
        print(f"  [progress] t={t_end/60:.1f}min: {unique_so_far} unique spots so far", flush=True)

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
        description='SparkGap — Open Source Linux CW Skimmer',
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
