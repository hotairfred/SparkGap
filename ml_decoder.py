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


def generate_cw_audio(text, wpm=25, freq=600, sample_rate=4000, noise_level=0.1):
    """Generate CW audio for a text string."""
    dit_duration = 1.2 / wpm  # seconds
    samples_per_dit = int(sample_rate * dit_duration)

    audio = []

    def tone(n):
        return [math.sin(2 * math.pi * freq * i / sample_rate) for i in range(n)]

    def silence(n):
        return [0.0] * n

    for i, char in enumerate(text.upper()):
        if char == ' ':
            audio.extend(silence(samples_per_dit * 4))  # word space (7 - 3 already added)
            continue
        if char not in MORSE:
            continue

        code = MORSE[char]
        for j, element in enumerate(code):
            if element == '.':
                audio.extend(tone(samples_per_dit))
            else:
                audio.extend(tone(samples_per_dit * 3))
            # Inter-element space
            if j < len(code) - 1:
                audio.extend(silence(samples_per_dit))

        # Inter-character space
        audio.extend(silence(samples_per_dit * 3))

    # Add noise
    audio = np.array(audio)
    noise = np.random.normal(0, noise_level, len(audio))
    audio = audio + noise

    return audio.astype(np.float32)


def generate_training_data(output_dir='training_data', num_samples=1000):
    """Generate synthetic CW training data with labels."""
    os.makedirs(output_dir, exist_ok=True)

    # Load master.scp for realistic callsigns
    callsigns = []
    if os.path.exists('MASTER.SCP'):
        with open('MASTER.SCP') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and len(line) >= 4:
                    callsigns.append(line)

    if not callsigns:
        callsigns = ['W1ABC', 'DL3XYZ', 'JA1ABC', 'VK2ABC', 'G4XYZ']

    labels = []

    for i in range(num_samples):
        # Random parameters
        wpm = np.random.randint(15, 40)
        noise_level = np.random.uniform(0.05, 0.5)
        freq = np.random.randint(400, 800)

        # Generate CQ/TEST message with random callsign
        call = callsigns[np.random.randint(0, len(callsigns))]
        patterns = [
            f"CQ TEST {call} {call}",
            f"CQ {call} {call}",
            f"TEST {call} TEST",
            f"{call} 5NN 28",
            f"CQ CQ {call}",
        ]
        text = patterns[np.random.randint(0, len(patterns))]

        # Generate audio
        audio = generate_cw_audio(text, wpm=wpm, freq=freq, noise_level=noise_level)

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
            'call': call,
            'wpm': wpm,
            'noise': float(noise_level),
            'freq': freq,
        })

        if (i + 1) % 100 == 0:
            print(f"  Generated {i+1}/{num_samples} samples", file=sys.stderr)

    # Save labels
    with open(os.path.join(output_dir, 'labels.json'), 'w') as f:
        json.dump(labels, f, indent=2)

    print(f"Generated {num_samples} training samples in {output_dir}/", file=sys.stderr)


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
