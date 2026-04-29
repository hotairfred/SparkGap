#!/usr/bin/env python3
"""
eval_uhsdr.py — Score UHSDR decoder against CW Skimmer answer key.

Extracts individual CW channels from wideband IQ recording,
runs UHSDR decoder on each, and scores against known callsigns.

Usage: python3 eval_uhsdr.py [--wpm N] [--bw N]
"""

import wave
import struct
import numpy as np
import subprocess
import re
import sys
import os
from collections import defaultdict

# Configuration
RECORDING = "DK3QN_40m_CW_contest_2009.wav"
ANSWER_KEY = "cwskimmer_spots.txt"
SCP_FILE = "COMBINED.SCP"
UHSDR_BIN = "./uhsdr_cw"
CW_TONE = 600        # Hz — decoder expects tone at this frequency
OUTPUT_RATE = 12000   # UHSDR decoder sample rate
CHANNEL_BW = 400      # Hz bandwidth for channel extraction

def load_answer_key(filename):
    """Parse CW Skimmer spot file to get freq_offset_kHz -> callsign mapping."""
    signals = {}
    pattern = re.compile(r'([-0-9.]+)\s+([A-Z0-9/]+)\s+(\d+)\s+dB\s+(\d+)\s+WPM')
    with open(filename) as f:
        for line in f:
            m = pattern.search(line)
            if m:
                freq_khz = float(m.group(1))
                call = m.group(2)
                snr = int(m.group(3))
                wpm = int(m.group(4))
                # Deduplicate: keep first occurrence (or highest SNR)
                if call not in signals or snr > signals[call][1]:
                    signals[call] = (freq_khz, snr, wpm)
    return signals

def load_scp(filename):
    """Load MASTER.SCP for validation."""
    calls = set()
    with open(filename) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                calls.add(line.upper())
    return calls

def load_iq(filename):
    """Load IQ recording as complex numpy array."""
    w = wave.open(filename, 'rb')
    assert w.getnchannels() == 2, "Expected stereo (IQ) recording"
    rate = w.getframerate()
    frames = w.readframes(w.getnframes())
    w.close()
    samples = np.frombuffer(frames, dtype=np.int16)
    i_samples = samples[0::2].astype(np.float64)
    q_samples = samples[1::2].astype(np.float64)
    return i_samples + 1j * q_samples, rate

def extract_channel(iq_data, sample_rate, freq_offset_hz, output_rate, tone_freq):
    """
    SSB demodulation: mix IQ down so the signal lands at tone_freq Hz,
    then lowpass filter and decimate to output_rate.
    """
    n = len(iq_data)
    t = np.arange(n) / sample_rate

    # Mix to put the signal at tone_freq Hz in the output
    # Signal is at freq_offset_hz in the IQ baseband
    # We want it at tone_freq after mixing
    mix_freq = freq_offset_hz - tone_freq
    mixed = iq_data * np.exp(-1j * 2 * np.pi * mix_freq * t)

    # Take real part (SSB demodulation)
    audio = mixed.real

    # Decimate from sample_rate to output_rate
    dec_factor = sample_rate // output_rate
    if dec_factor > 1:
        # Simple lowpass: moving average then decimate
        # Use a proper FIR for better results
        from scipy.signal import decimate as scipy_decimate
        try:
            audio = scipy_decimate(audio, dec_factor, ftype='fir', n=63)
        except ImportError:
            # Fallback: simple decimation
            audio = audio[::dec_factor]

    # Normalize to 16-bit range
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak * 16000

    return audio.astype(np.int16)

def run_uhsdr(audio_data, rate=12000, wpm=0):
    """Run UHSDR decoder on audio data, return decoded text."""
    # Write to raw PCM
    pcm = audio_data.tobytes()

    cmd = [UHSDR_BIN, "-r", str(rate), "-f", str(CW_TONE)]
    if wpm > 0:
        cmd += ["-s", str(wpm)]

    proc = subprocess.run(cmd, input=pcm, capture_output=True, timeout=30)
    return proc.stdout.decode('utf-8', errors='replace').strip()

def extract_callsigns(text):
    """Extract potential callsigns from decoded text."""
    CALL_RE = re.compile(r'[A-Z0-9]{1,2}\d{1,4}[A-Z]{1,4}')
    text = text.upper()
    found = set()
    for m in CALL_RE.finditer(text):
        call = m.group(0)
        if len(call) >= 3:
            found.add(call)
    return found

def main():
    fixed_wpm = 0
    for arg in sys.argv[1:]:
        if arg.startswith('--wpm='):
            fixed_wpm = int(arg.split('=')[1])

    print(f"=== UHSDR Decoder Evaluation ===")
    print(f"Recording: {RECORDING}")
    print(f"WPM: {'auto' if fixed_wpm == 0 else fixed_wpm}")
    print()

    # Load data
    print("Loading answer key...", end=" ", flush=True)
    signals = load_answer_key(ANSWER_KEY)
    print(f"{len(signals)} unique callsigns")

    print("Loading SCP database...", end=" ", flush=True)
    scp = load_scp(SCP_FILE)
    print(f"{len(scp)} calls")

    print("Loading IQ recording...", end=" ", flush=True)
    iq_data, sample_rate = load_iq(RECORDING)
    duration = len(iq_data) / sample_rate
    print(f"{duration:.1f}s at {sample_rate} Hz")
    print()

    # Process each signal
    correct = []
    partial = []
    missed = []
    false_pos = []

    print(f"{'CALL':<12} {'FREQ':>7} {'SNR':>4} {'WPM':>4}  {'RESULT':<8} DECODED TEXT")
    print("-" * 90)

    for call in sorted(signals.keys(), key=lambda c: signals[c][0]):
        freq_khz, snr, wpm = signals[call]
        freq_hz = freq_khz * 1000  # Convert kHz offset to Hz

        # Extract channel audio
        audio = extract_channel(iq_data, sample_rate, freq_hz, OUTPUT_RATE, CW_TONE)

        # Run decoder
        use_wpm = fixed_wpm if fixed_wpm > 0 else 0
        decoded = run_uhsdr(audio, OUTPUT_RATE, use_wpm)

        # Clean up decoded text for display
        decoded_clean = decoded.replace('\n', ' ').strip()
        # Remove the "uhsdr_cw:" prefix lines
        lines = [l for l in decoded.split('\n') if not l.startswith('uhsdr_cw')]
        decoded_text = ' '.join(lines).strip()

        # Check if the target callsign appears in decoded text
        decoded_upper = decoded_text.upper()

        if call in decoded_upper:
            result = "HIT"
            correct.append(call)
        elif any(c in decoded_upper for c in [call[:3], call[-3:]]):
            result = "PARTIAL"
            partial.append(call)
        else:
            result = "MISS"
            missed.append(call)

        # Truncate decoded text for display
        display_text = decoded_text[:60] if len(decoded_text) > 60 else decoded_text
        print(f"{call:<12} {freq_khz:>6.1f}k {snr:>3}dB {wpm:>3}wpm  {result:<8} {display_text}")

    # Summary
    total = len(signals)
    print()
    print(f"=== RESULTS ===")
    print(f"Total signals:  {total}")
    print(f"Correct (HIT):  {len(correct)} ({100*len(correct)/total:.1f}%)")
    print(f"Partial:        {len(partial)} ({100*len(partial)/total:.1f}%)")
    print(f"Missed:         {len(missed)} ({100*len(missed)/total:.1f}%)")
    print()

    if correct:
        print(f"Correct calls: {' '.join(sorted(correct))}")
    if partial:
        print(f"Partial calls: {' '.join(sorted(partial))}")

if __name__ == '__main__':
    main()
