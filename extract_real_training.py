#!/usr/bin/env python3
"""
extract_real_training.py — Extract labeled CW training data from real recordings.

Uses bmorse speed sweep output + answer key to create labeled audio segments
from real channelized recordings. Closes the synthetic→real domain gap.

Usage: python3 extract_real_training.py <recording.wav> <bmorse_output.txt> [--answer-key calls.txt]
"""

import numpy as np
import wave
import re
import os
import json
import sys
from eval_model import channelize

CALL_RE = re.compile(r'(?<![A-Z0-9])([A-Z0-9]{1,2}\d{1,2}[A-Z]{1,3})(?![A-Z0-9])')


def extract_training_samples(wav_path, bmorse_outputs, answer_key=None,
                              output_dir='training_data_real', target_rate=4000,
                              segment_duration=4.0):
    """Extract labeled audio segments from a real recording.

    For each frequency where bmorse found a valid callsign, extract the
    channelized audio as a training sample with the callsign as the label.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Read recording
    print(f"Loading {wav_path}...", file=sys.stderr)
    w = wave.open(wav_path, 'rb')
    sr = w.getframerate()
    frames = w.readframes(w.getnframes())
    w.close()
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    duration = len(samples) / sr
    print(f"  {len(samples)} samples, {sr} Hz, {duration:.1f}s", file=sys.stderr)

    # Load master.scp for validation
    master = set()
    for scp_file in ['MASTER.SCP', 'add_calls.txt']:
        if os.path.exists(scp_file):
            with open(scp_file) as f:
                for line in f:
                    l = line.strip().upper()
                    if l and not l.startswith('#'):
                        master.add(l)

    # Parse bmorse outputs — extract freq:wpm:text lines
    # Find callsigns that appear at consistent frequencies
    from collections import defaultdict
    call_freqs = defaultdict(list)  # call -> [(freq, wpm, text), ...]

    for bmorse_file in bmorse_outputs:
        if not os.path.exists(bmorse_file):
            continue
        with open(bmorse_file) as f:
            for line in f:
                line = line.strip()
                if not line or ':' not in line:
                    continue
                parts = line.split(':', 2)
                if len(parts) < 3:
                    continue
                try:
                    freq = float(parts[0])
                    wpm = int(parts[1])
                except (ValueError, IndexError):
                    continue
                text = parts[2].upper()
                for m in CALL_RE.finditer(text):
                    call = m.group(1)
                    if len(call) >= 4 and call in master:
                        call_freqs[call].append((freq, wpm, text))

    # Filter to high-confidence calls (3+ sightings or in answer key)
    confident_calls = {}
    for call, sightings in call_freqs.items():
        if answer_key and call in answer_key:
            confident_calls[call] = sightings
        elif len(sightings) >= 3:
            confident_calls[call] = sightings

    print(f"Found {len(confident_calls)} confident callsigns", file=sys.stderr)

    # Extract audio segments
    labels = []
    sample_idx = 0
    segment_samples = int(segment_duration * target_rate)

    # PSD for peak finding
    fft_size = 8192
    window = np.hanning(fft_size)
    psd = np.zeros(fft_size // 2 + 1)
    n_frames = min(20, len(samples) // fft_size)
    for i in range(n_frames):
        frame = samples[i * fft_size:(i + 1) * fft_size] * window
        psd += np.abs(np.fft.rfft(frame)) ** 2
    psd /= n_frames
    freq_res = sr / fft_size

    # Skip pileup frequencies (CY0S at ~67000 Hz + stations calling UP)
    # Tight exclusion: just CY0S TX freq + UP pileup, preserve CWT on either side
    PILEUP_LOW = 66500
    PILEUP_HIGH = 70000

    for call, sightings in confident_calls.items():
        # Find the most common frequency for this call
        from collections import Counter
        freq_counter = Counter(int(s[0]) for s in sightings)
        best_freq = freq_counter.most_common(1)[0][0]

        # Skip pileup area
        if PILEUP_LOW <= best_freq <= PILEUP_HIGH:
            continue

        # Find exact peak
        lo_bin = max(0, int((best_freq - 100) / freq_res))
        hi_bin = min(len(psd) - 1, int((best_freq + 100) / freq_res))
        if hi_bin <= lo_bin:
            continue
        peak_bin = lo_bin + np.argmax(psd[lo_bin:hi_bin])
        exact_freq = peak_bin * freq_res

        # Channelize
        try:
            channel = channelize(samples, sr, exact_freq, target_rate=target_rate)
        except:
            continue

        # Normalize
        peak = np.max(np.abs(channel))
        if peak < 1e-6:
            continue
        channel = channel * 0.9 / peak

        # Compute envelope for activity detection (only label windows with signal)
        from scipy.signal import hilbert
        envelope = np.abs(hilbert(channel))
        # Smooth envelope over ~50ms windows
        smooth_len = int(target_rate * 0.05)
        if smooth_len > 1:
            kernel = np.ones(smooth_len) / smooth_len
            envelope = np.convolve(envelope, kernel, mode='same')
        env_threshold = np.median(envelope) + 0.5 * (np.max(envelope) - np.median(envelope))

        # Extract sliding windows — only keep windows with active signal
        hop = segment_samples // 2  # 50% overlap
        for start in range(0, len(channel) - segment_samples, hop):
            segment = channel[start:start + segment_samples]
            seg_envelope = envelope[start:start + segment_samples]

            # Skip if too quiet (no signal in this window)
            if np.max(np.abs(segment)) < 0.1:
                continue

            # Skip if signal is active less than 20% of the window
            # (probably between transmissions, just noise)
            active_pct = np.sum(seg_envelope > env_threshold) / len(seg_envelope)
            if active_pct < 0.10:
                continue

            # Write as WAV
            wav_name = f'real_{sample_idx:06d}.wav'
            wav_path_out = os.path.join(output_dir, wav_name)
            wout = wave.open(wav_path_out, 'wb')
            wout.setnchannels(1)
            wout.setsampwidth(2)
            wout.setframerate(target_rate)
            audio_int = (segment * 32767).clip(-32768, 32767).astype(np.int16)
            wout.writeframes(audio_int.tobytes())
            wout.close()

            labels.append({
                'file': wav_name,
                'text': call,  # Just the callsign — bmorse doesn't give us the full exchange
                'callsign': call,
                'freq': float(exact_freq),
                'source': 'real_cwt',
                'wpm': sightings[0][1],
            })
            sample_idx += 1

        if sample_idx % 100 == 0 and sample_idx > 0:
            print(f"  {sample_idx} segments extracted ({len(labels)} labels)", file=sys.stderr)

    # Save labels
    labels_path = os.path.join(output_dir, 'labels.json')
    with open(labels_path, 'w') as f:
        json.dump(labels, f, indent=2)

    print(f"\nExtracted {len(labels)} training segments from {len(confident_calls)} callsigns",
          file=sys.stderr)
    print(f"Saved to {output_dir}/", file=sys.stderr)
    return labels


if __name__ == '__main__':
    import glob

    wav = sys.argv[1] if len(sys.argv) > 1 else '/tmp/cwt_15min.wav'
    bmorse_files = sorted(glob.glob('bmorse_cpp_s*.txt'))
    print(f"bmorse output files: {bmorse_files}", file=sys.stderr)

    # Load answer key
    answer_key = set('9Y4D AA3B AA4NP AA6G AD4UB AI5IN AJ6V CY0S DF7TV EB1EOE F8NHF G3LDI HA7NZ HA9RE HZ1TT I1MMR IK4QJF K0AWU K0CDJ K0IS K0JM K1BZ K1DW K1GU K1HZ K2AR K2LE K3FI K3JT K4IU K5DXR K5PE K5TN K5YC K5YCM K6RAD K8WWS K9MA KB2BK KB4EKK KD0RC KD4JG KE2D KH6M KI7MD KM0O KM9R KV0I KW7Q M2RQ M7JET N2CG N2EY N3AD N3JT N4GO N5AW N5JJ N5NA N5XZ N7DEY N7UA N9FZ ND9M NJ6Q NN7M NQ5P NT5V NT6Q NY6C OH5RF OM2XW ON4TH PA3AAV PY2NA R6JY RD3R RK3Q S55DX S5SH SP7NHS TG9ADM UN6ZZI UR5EN VE3KIU VE6JF VE7WO VE7ZO W0EAS W0PAB W0TG W1QK W1TO W2GD W2NMI W3US W4CMG W4IT W4SPR W5JMW W5RY W5TM W6AJR W6IWI W7JET W7MTL W8EH W8XAL W9CF W9ILY WA0I WA0T WA5RML WB0OQV WB2AA WR7T WU6P ZA1EM'.split())

    extract_training_samples(wav, bmorse_files, answer_key=answer_key)
