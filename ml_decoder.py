#!/usr/bin/env python3
"""
ml_decoder.py — ML-based CW decoder for the multi-pass pipeline.

Phase 1: Spectrogram-based approach with trainable model.
Generates synthetic CW training data, trains a small model,
then decodes real signals.

Usage:
    python3 ml_decoder.py train          # Generate data and train
    python3 ml_decoder.py decode <wav>   # Decode a WAV file
    python3 ml_decoder.py generate       # Just generate training data
"""

import numpy as np
import sys
import os
import json
import wave
import struct
import math
from collections import defaultdict

# Morse code dictionary
MORSE = {
    'A': '.-',    'B': '-...',  'C': '-.-.',  'D': '-..',
    'E': '.',     'F': '..-.',  'G': '--.',   'H': '....',
    'I': '..',    'J': '.---',  'K': '-.-',   'L': '.-..',
    'M': '--',    'N': '-.',    'O': '---',   'P': '.--.',
    'Q': '--.-',  'R': '.-.',   'S': '...',   'T': '-',
    'U': '..-',   'V': '...-',  'W': '.--',   'X': '-..-',
    'Y': '-.--',  'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--',
    '4': '....-', '5': '.....', '6': '-....', '7': '--...',
    '8': '---..', '9': '----.',
    '/': '-..-.',
}

REVERSE_MORSE = {v: k for k, v in MORSE.items()}


def generate_cw_audio(text, wpm=25, freq=600, sample_rate=4000, noise_level=0.1,
                      fist_jitter=0.0, qsb_depth=0.0, qsb_rate=0.2,
                      rise_time_ms=5.0):
    """Generate CW audio for a text string with realistic imperfections.

    Args:
        fist_jitter: 0.0-0.4 — random variation in element timing (sloppy fist)
        qsb_depth: 0.0-0.8 — depth of slow amplitude fading
        qsb_rate: Hz rate of QSB fading
        rise_time_ms: keying rise/fall time in milliseconds
    """
    dit_duration = 1.2 / wpm  # seconds
    samples_per_dit = int(sample_rate * dit_duration)
    rise_samples = int(sample_rate * rise_time_ms / 1000.0)

    # Phase-continuous tone generation
    phase = 0.0
    phase_inc = 2 * math.pi * freq / sample_rate

    def jittered(n):
        """Apply timing jitter to element length."""
        if fist_jitter > 0:
            return max(int(n * (1.0 + np.random.uniform(-fist_jitter, fist_jitter))), 1)
        return n

    def shaped_tone(n):
        """Generate tone with raised-cosine rise/fall shaping."""
        nonlocal phase
        rise = min(rise_samples, n // 3)
        samples = np.zeros(n)
        for i in range(n):
            # Envelope shaping
            if i < rise:
                env = 0.5 * (1.0 - math.cos(math.pi * i / rise))
            elif i >= n - rise:
                env = 0.5 * (1.0 - math.cos(math.pi * (n - i) / rise))
            else:
                env = 1.0
            samples[i] = env * math.sin(phase)
            phase += phase_inc
        return samples

    def silence(n):
        return np.zeros(n)

    segments = []

    for i, char in enumerate(text.upper()):
        if char == ' ':
            segments.append(silence(jittered(samples_per_dit * 4)))
            continue
        if char not in MORSE:
            continue

        code = MORSE[char]
        for j, element in enumerate(code):
            if element == '.':
                segments.append(shaped_tone(jittered(samples_per_dit)))
            else:
                segments.append(shaped_tone(jittered(samples_per_dit * 3)))
            if j < len(code) - 1:
                segments.append(silence(jittered(samples_per_dit)))

        segments.append(silence(jittered(samples_per_dit * 3)))

    audio = np.concatenate(segments) if segments else np.zeros(100)

    # QSB fading
    if qsb_depth > 0:
        t = np.arange(len(audio), dtype=np.float64) / sample_rate
        qsb_phase = np.random.uniform(0, 2 * math.pi)
        fading = 1.0 - qsb_depth * 0.5 * (1.0 + np.sin(2 * math.pi * qsb_rate * t + qsb_phase))
        audio = audio * fading

    # Add noise
    noise = np.random.normal(0, noise_level, len(audio))
    audio = audio + noise

    return audio.astype(np.float32)


def generate_training_data(output_dir='training_data', num_samples=1000):
    """Generate synthetic CW training data with realistic variety.

    Improvements over v1:
    - 15 exchange patterns (CQ, TEST, DE, QRZ, RST, serial numbers, bare calls)
    - Slash calls (10% of samples)
    - Short calls (3-4 chars naturally included)
    - Sloppy fist timing jitter (0-30%)
    - QSB fading on 30% of samples
    - Wider WPM range (10-45)
    - Wider noise range (0.01-1.0)
    - Wider frequency range (300-1200 Hz)
    - Rise/fall keying shape (2-10ms)
    """
    os.makedirs(output_dir, exist_ok=True)

    # Load master.scp for realistic callsigns
    callsigns = []
    short_calls = []  # 3-4 char calls
    long_calls = []   # 6+ char calls
    slash_calls = []  # portable/reciprocal calls

    if os.path.exists('MASTER.SCP'):
        with open('MASTER.SCP') as f:
            for line in f:
                line = line.strip().upper()
                if line and not line.startswith('#') and len(line) >= 3:
                    callsigns.append(line)
                    if len(line) <= 4:
                        short_calls.append(line)
                    elif len(line) >= 6:
                        long_calls.append(line)

    if not callsigns:
        callsigns = ['W1ABC', 'DL3XYZ', 'JA1ABC', 'VK2ABC', 'G4XYZ']

    # Generate some slash calls from existing callsigns
    suffixes = ['/P', '/M', '/QRP', '/1', '/2', '/3', '/4', '/5', '/6', '/7', '/8', '/9', '/0']
    prefixes_for_slash = ['F', 'DL', 'G', 'I', 'EA', 'OH', 'SM', 'OZ', 'ON', 'PA', 'HB9']
    for call in callsigns[:500]:
        if '/' not in call and len(call) >= 4:
            if np.random.random() < 0.1:
                slash_calls.append(call + suffixes[np.random.randint(0, len(suffixes))])
            if np.random.random() < 0.05:
                pfx = prefixes_for_slash[np.random.randint(0, len(prefixes_for_slash))]
                slash_calls.append(f"{pfx}/{call}")

    def estimate_cw_duration(text, wpm):
        """Estimate CW audio duration in seconds for given text and WPM."""
        dit = 1.2 / wpm
        total_elements = 0
        for i, char in enumerate(text.upper()):
            if char == ' ':
                total_elements += 4  # word space (7 - 3 already counted)
            elif char in MORSE:
                code = MORSE[char]
                for j, el in enumerate(code):
                    total_elements += 3 if el == '-' else 1
                    if j < len(code) - 1:
                        total_elements += 1  # inter-element
                total_elements += 3  # inter-character
        return total_elements * dit

    # Maximum audio duration that fits in 768 spec frames at 4kHz/hop=32
    MAX_DURATION = 768 * 32 / 4000.0  # ~6.1 seconds

    # Contest serial numbers
    def random_serial():
        """Generate a contest serial number."""
        return str(np.random.randint(1, 2000)).zfill(np.random.choice([0, 2, 3, 4]))

    def random_rst():
        """Generate a realistic RST."""
        return np.random.choice(['599', '5NN', '579', '589', '559', '549',
                                  '339', '449', '569', '5NN'])

    def random_zone():
        """CQ zone number."""
        return str(np.random.randint(1, 41)).zfill(2)

    labels = []

    for i in range(num_samples):
        # Random parameters — wider ranges
        wpm = np.random.randint(10, 46)
        noise_level = np.random.uniform(0.01, 1.0)
        freq = np.random.randint(300, 1201)

        # Fist quality: 70% clean, 20% slightly sloppy, 10% very sloppy
        r = np.random.random()
        if r < 0.70:
            fist_jitter = np.random.uniform(0.0, 0.05)
        elif r < 0.90:
            fist_jitter = np.random.uniform(0.05, 0.15)
        else:
            fist_jitter = np.random.uniform(0.15, 0.30)

        # QSB fading on 30% of samples
        if np.random.random() < 0.30:
            qsb_depth = np.random.uniform(0.1, 0.6)
            qsb_rate = np.random.uniform(0.1, 0.5)
        else:
            qsb_depth = 0.0
            qsb_rate = 0.2

        # Rise time: 2-10ms
        rise_time = np.random.uniform(2.0, 10.0)

        # Pick callsign — 10% slash, 15% short, rest normal
        r = np.random.random()
        if r < 0.10 and slash_calls:
            call = slash_calls[np.random.randint(0, len(slash_calls))]
        elif r < 0.25 and short_calls:
            call = short_calls[np.random.randint(0, len(short_calls))]
        else:
            call = callsigns[np.random.randint(0, len(callsigns))]

        # Pick a second callsign for QSO-style exchanges
        other_call = callsigns[np.random.randint(0, len(callsigns))]

        # Patterns grouped by length — pick from longest that fits
        long_patterns = [
            f"CQ TEST {call} {call}",
            f"CQ CQ CQ DE {call} {call}",
            f"CQ CQ CQ DE {call} {call} K",
            f"{other_call} DE {call} {random_rst()} {random_zone()}",
            f"{other_call} {call} {random_rst()} {random_serial()}",
        ]
        medium_patterns = [
            f"CQ {call} {call}",
            f"CQ CQ {call}",
            f"TEST {call} TEST",
            f"QRZ DE {call} {call}",
            f"DE {call} {call} K",
            f"{call} {random_rst()} {random_zone()}",
            f"{call} {random_rst()} {random_serial()}",
            f"R {random_rst()} {random_zone()} {call}",
        ]
        short_patterns = [
            f"{call} {call}",
            f"TU {call}",
            f"CQ {call}",
            f"{call} {random_rst()}",
        ]
        minimal_patterns = [
            f"{call}",
        ]

        # Collect all patterns that fit in the window, pick randomly
        all_patterns = long_patterns + medium_patterns + short_patterns + minimal_patterns
        fitting = [p for p in all_patterns if estimate_cw_duration(p, wpm) <= MAX_DURATION * 0.95]
        if not fitting:
            fitting = minimal_patterns
        text = fitting[np.random.randint(0, len(fitting))]

        # Generate audio with all the bells and whistles
        audio = generate_cw_audio(text, wpm=wpm, freq=freq, noise_level=noise_level,
                                  fist_jitter=fist_jitter, qsb_depth=qsb_depth,
                                  qsb_rate=qsb_rate, rise_time_ms=rise_time)

        # Save as WAV
        wav_path = os.path.join(output_dir, f'sample_{i:05d}.wav')
        with wave.open(wav_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(4000)
            int_audio = (audio * 16000).clip(-32768, 32767).astype(np.int16)
            wf.writeframes(int_audio.tobytes())

        labels.append({
            'file': f'sample_{i:05d}.wav',
            'text': text,
            'callsign': call,
            'wpm': int(wpm),
            'noise': float(noise_level),
            'freq': int(freq),
            'jitter': float(fist_jitter),
            'qsb': float(qsb_depth),
        })

        if (i + 1) % 1000 == 0:
            print(f"  Generated {i+1}/{num_samples} samples", file=sys.stderr)

    # Save labels
    with open(os.path.join(output_dir, 'labels.json'), 'w') as f:
        json.dump(labels, f, indent=2)

    print(f"Generated {num_samples} training samples in {output_dir}/", file=sys.stderr)
    print(f"  Slash calls in pool: {len(slash_calls)}", file=sys.stderr)
    print(f"  Short calls (3-4): {len(short_calls)}", file=sys.stderr)
    print(f"  Total call pool: {len(callsigns)}", file=sys.stderr)


def audio_to_spectrogram(audio, sample_rate=4000, fft_size=128, hop=32):
    """Convert audio to spectrogram."""
    n_frames = (len(audio) - fft_size) // hop + 1
    window = np.hanning(fft_size)

    spec = np.zeros((n_frames, fft_size // 2))
    for i in range(n_frames):
        frame = audio[i * hop: i * hop + fft_size] * window
        fft = np.abs(np.fft.rfft(frame))[:-1]
        spec[i] = fft

    # Log scale
    spec = np.log1p(spec)
    return spec


def decode_with_envelope(audio, sample_rate=48000, freq_hz=0, bandwidth=50):
    """
    Simple envelope-based CW decoder using numpy.
    Extracts on/off keying pattern and converts to Morse.
    """
    # Compute power spectrum to find the signal
    fft_size = sample_rate // bandwidth
    n_frames = len(audio) // (fft_size // 4)  # 4x overlap

    if n_frames < 10:
        return ""

    # Compute overlapping FFT magnitudes for the target bin
    hop = fft_size // 4
    bin_idx = int(freq_hz / bandwidth) if freq_hz > 0 else None

    powers = []
    for i in range(n_frames):
        start = i * hop
        end = start + fft_size
        if end > len(audio):
            break
        frame = audio[start:end] * np.hanning(fft_size)
        fft = np.abs(np.fft.rfft(frame))

        if bin_idx is not None and bin_idx < len(fft):
            powers.append(fft[bin_idx])
        else:
            # Find strongest bin
            powers.append(np.max(fft[1:]))

    if not powers:
        return ""

    powers = np.array(powers)

    # Adaptive threshold
    threshold = np.median(powers) + (np.max(powers) - np.median(powers)) * 0.4

    # Convert to binary
    binary = (powers > threshold).astype(int)

    # Find runs of 1s and 0s
    changes = np.diff(binary)
    mark_starts = np.where(changes == 1)[0]
    mark_ends = np.where(changes == -1)[0]

    if len(mark_starts) == 0 or len(mark_ends) == 0:
        return ""

    # Align starts and ends
    if mark_ends[0] < mark_starts[0]:
        mark_ends = mark_ends[1:]
    min_len = min(len(mark_starts), len(mark_ends))
    mark_starts = mark_starts[:min_len]
    mark_ends = mark_ends[:min_len]

    if min_len < 3:
        return ""

    # Compute element durations (in frames)
    mark_durations = mark_ends - mark_starts
    space_durations = mark_starts[1:] - mark_ends[:-1]

    # Estimate dit length from shortest marks
    sorted_marks = np.sort(mark_durations)
    dit_estimate = np.median(sorted_marks[:max(len(sorted_marks) // 3, 1)])
    if dit_estimate < 1:
        dit_estimate = 2

    # Classify elements
    morse_string = ""
    text = ""
    code = ""

    for i in range(min_len):
        # Classify mark as dit or dah
        if mark_durations[i] < dit_estimate * 2:
            code += "."
        else:
            code += "-"

        # Check space after (if not last)
        if i < len(space_durations):
            if space_durations[i] > dit_estimate * 5:
                # Word space
                if code in REVERSE_MORSE:
                    text += REVERSE_MORSE[code]
                text += " "
                code = ""
            elif space_durations[i] > dit_estimate * 2:
                # Character space
                if code in REVERSE_MORSE:
                    text += REVERSE_MORSE[code]
                code = ""

    # Final character
    if code in REVERSE_MORSE:
        text += REVERSE_MORSE[code]

    return text.strip()


def decode_wav_multiband(wav_path, sample_rate=48000, bandwidth=50):
    """Decode a WAV file across all frequency bins."""
    # Read WAV
    w = wave.open(wav_path, 'rb')
    nch = w.getnchannels()
    data = w.readframes(w.getnframes())
    w.close()

    samples = np.array(struct.unpack('<' + 'h' * (len(data) // 2), data), dtype=np.float32)

    if nch == 2:
        # Extract I channel
        samples = samples[0::2]

    samples /= 32768.0

    # Find active bins
    fft_size = sample_rate // bandwidth
    num_bins = fft_size // 2

    # Quick power scan
    frame = samples[:fft_size] * np.hanning(fft_size)
    fft = np.abs(np.fft.rfft(frame))
    noise_floor = np.median(fft)
    threshold = noise_floor * 4.0

    active_bins = np.where(fft > threshold)[0]

    results = []
    for bin_idx in active_bins:
        freq_hz = bin_idx * bandwidth
        text = decode_with_envelope(samples, sample_rate, freq_hz, bandwidth)
        if text and len(text) >= 4:
            # Estimate WPM from text length and duration
            wpm = 20  # rough estimate
            results.append(f"{freq_hz}:{wpm}:{text}")

    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: ml_decoder.py [generate|train|decode] [args]")
        return

    cmd = sys.argv[1]

    if cmd == 'generate':
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 500
        generate_training_data(num_samples=n)

    elif cmd == 'decode':
        if len(sys.argv) < 3:
            print("Usage: ml_decoder.py decode <wav_file> [bandwidth]")
            return
        wav = sys.argv[2]
        bw = int(sys.argv[3]) if len(sys.argv) > 3 else 50

        print(f"Decoding {wav} with {bw} Hz bins...", file=sys.stderr)
        results = decode_wav_multiband(wav, bandwidth=bw)
        for r in results:
            print(r)
        print(f"Found {len(results)} signals", file=sys.stderr)

    elif cmd == 'train':
        print("Training not yet implemented — using envelope decoder", file=sys.stderr)
        print("Run 'generate' first to create training data", file=sys.stderr)

    else:
        print(f"Unknown command: {cmd}")


if __name__ == '__main__':
    main()
