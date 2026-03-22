#!/usr/bin/env python3
"""
eval_uhsdr_cwt.py — Score UHSDR decoder on CWT 40m recording (minutes 15-30).

1. Load 15 min of 192kHz IQ from B1_20260319_030000_7090kHz.wav
2. FFT to find CW signals (peaks in the spectrum)
3. Extract each signal as a narrowband SSB channel
4. Run UHSDR decoder on each
5. Score against 118-call answer key

Usage: python3 eval_uhsdr_cwt.py [--chunk-minutes N] [--threshold N]
"""

import struct
import numpy as np
import subprocess
import re
import sys
from scipy.signal import decimate as scipy_decimate

RECORDING = "B1_20260319_030000_7090kHz.wav"
UHSDR_BIN = "./uhsdr_cw"
CW_TONE = 600
OUTPUT_RATE = 12000
CENTER_FREQ_KHZ = 7090  # Recording center frequency

# 118-call answer key (minutes 15-30)
ANSWER_KEY = set("""9Y4D,AA3B,AA4NP,AA6G,AD4UB,AI5IN,AJ6V,CY0S,DF7TV,EB1EOE,F8NHF,G3LDI,HA7NZ,HA9RE,HZ1TT,I1MMR,IK4QJF,K0AWU,K0CDJ,K0IS,K0JM,K1BZ,K1DW,K1GU,K1HZ,K2AR,K2LE,K3FI,K3JT,K4IU,K5DXR,K5PE,K5TN,K5YC,K5YCM,K6RAD,K8WWS,K9MA,KB2BK,KB4EKK,KD0RC,KD4JG,KE2D,KH6M,KI7MD,KM0O,KM9R,KV0I,KW7Q,M2RQ,M7JET,N2CG,N2EY,N3AD,N3JT,N4GO,N5AW,N5JJ,N5NA,N5XZ,N7DEY,N7UA,N9FZ,ND9M,NJ6Q,NN7M,NQ5P,NT5V,NT6Q,NY6C,OH5RF,OM2XW,ON4TH,PA3AAV,PY2NA,R6JY,RD3R,RK3Q,S55DX,S5SH,SP7NHS,TG9ADM,UN6ZZI,UR5EN,VE3KIU,VE6JF,VE7WO,VE7ZO,W0EAS,W0PAB,W0TG,W1QK,W1TO,W2GD,W2NMI,W3US,W4CMG,W4IT,W4SPR,W5JMW,W5RY,W5TM,W6AJR,W6IWI,W7JET,W7MTL,W8EH,W8XAL,W9CF,W9ILY,WA0I,WA0T,WA5RML,WB0OQV,WB2AA,WR7T,WU6P,ZA1EM""".strip().split(","))

# Load SCP
def load_scp(filename='COMBINED.SCP'):
    calls = set()
    with open(filename) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                calls.add(line.upper())
    return calls

def read_24bit_iq_chunk(filename, start_sec, duration_sec, rate=192000):
    """Read a chunk of 24-bit stereo IQ from WAV extensible format."""
    channels = 2
    bytes_per_sample = 3  # 24-bit
    bytes_per_frame = bytes_per_sample * channels  # 6 bytes per frame

    with open(filename, 'rb') as f:
        # Find data chunk
        f.read(12)  # RIFF header
        while True:
            chunk_id = f.read(4)
            chunk_size = struct.unpack('<I', f.read(4))[0]
            if chunk_id == b'data':
                data_offset = f.tell()
                break
            f.seek(chunk_size, 1)

        # Seek to start position
        start_frame = int(start_sec * rate)
        n_frames = int(duration_sec * rate)
        f.seek(data_offset + start_frame * bytes_per_frame)

        # Read raw bytes
        raw = f.read(n_frames * bytes_per_frame)

    # Convert 24-bit to float64
    n_samples = len(raw) // 3
    samples = np.zeros(n_samples, dtype=np.float64)
    for i in range(n_samples):
        b = raw[i*3:(i+1)*3]
        val = int.from_bytes(b, byteorder='little', signed=False)
        if val >= 0x800000:
            val -= 0x1000000
        samples[i] = val

    # Deinterleave I/Q
    i_ch = samples[0::2]
    q_ch = samples[1::2]
    return i_ch + 1j * q_ch

def find_cw_signals(iq_data, sample_rate, fft_size=8192, threshold_db=10):
    """Find CW signal peaks in the spectrum."""
    # Average multiple FFTs for a clean spectrum
    n_ffts = min(len(iq_data) // fft_size, 200)
    avg_spectrum = np.zeros(fft_size)

    for i in range(n_ffts):
        chunk = iq_data[i * fft_size:(i + 1) * fft_size]
        spectrum = np.abs(np.fft.fft(chunk * np.hanning(fft_size))) ** 2
        avg_spectrum += spectrum

    avg_spectrum /= n_ffts
    avg_spectrum_db = 10 * np.log10(avg_spectrum + 1e-20)

    # Frequency axis
    freqs = np.fft.fftfreq(fft_size, 1.0 / sample_rate)

    # Only look at CW sub-band: roughly 7000-7060 kHz
    # Center is 7090 kHz, so CW is at offset -90 to -30 kHz
    # But CWT stations might be anywhere in 7000-7125
    # Use full bandwidth but focus on reasonable CW range

    # Find peaks above noise floor
    # Estimate noise floor as median
    noise_floor = np.median(avg_spectrum_db)
    peak_threshold = noise_floor + threshold_db

    # Find local maxima above threshold
    signals = []
    min_spacing = int(200 * fft_size / sample_rate)  # 200 Hz minimum spacing

    # Sort by power, find peaks
    above = np.where(avg_spectrum_db > peak_threshold)[0]
    if len(above) == 0:
        return []

    # Cluster nearby bins
    clusters = []
    current_cluster = [above[0]]
    for i in range(1, len(above)):
        if above[i] - above[i-1] <= min_spacing:
            current_cluster.append(above[i])
        else:
            clusters.append(current_cluster)
            current_cluster = [above[i]]
    clusters.append(current_cluster)

    for cluster in clusters:
        peak_bin = cluster[np.argmax(avg_spectrum_db[cluster])]
        freq_hz = freqs[peak_bin]
        power_db = avg_spectrum_db[peak_bin] - noise_floor
        signals.append((freq_hz, power_db))

    # Sort by frequency
    signals.sort(key=lambda x: x[0])
    return signals

def extract_and_decode(iq_data, sample_rate, freq_offset_hz, wpm=0):
    """SSB demod + UHSDR decode for a single signal."""
    n = len(iq_data)
    t = np.arange(n) / sample_rate

    # Mix to put signal at CW_TONE Hz
    mix_freq = freq_offset_hz - CW_TONE
    mixed = iq_data * np.exp(-1j * 2 * np.pi * mix_freq * t)
    audio = mixed.real

    # Decimate 192000 -> 12000 (factor 16)
    audio = scipy_decimate(audio, 16, ftype='fir', n=63)

    # Normalize
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak * 16000
    audio = audio.astype(np.int16)

    # Run decoder
    cmd = [UHSDR_BIN, "-r", str(OUTPUT_RATE), "-f", str(CW_TONE)]
    if wpm > 0:
        cmd += ["-s", str(wpm)]

    proc = subprocess.run(cmd, input=audio.tobytes(), capture_output=True, timeout=60)
    out = proc.stdout.decode('utf-8', errors='replace').strip()
    lines = [l for l in out.split('\n') if not l.startswith('uhsdr_cw')]
    return ' '.join(lines).strip()

def extract_callsigns(text):
    """Extract potential callsigns from decoded text."""
    CALL_RE = re.compile(r'[A-Z0-9]{1,3}\d{1,4}[A-Z]{1,4}')
    # Also match slash calls
    SLASH_RE = re.compile(r'[A-Z0-9]{1,4}/[A-Z0-9]{1,3}\d[A-Z]{1,3}')
    text = text.upper()
    found = set()
    for m in SLASH_RE.finditer(text):
        found.add(m.group(0))
    for m in CALL_RE.finditer(text):
        call = m.group(0)
        if len(call) >= 3:
            found.add(call)
    return found

def main():
    chunk_minutes = 5  # Process in 5-minute chunks to manage memory
    threshold_db = 8
    multi_speed = '--multi-speed' in sys.argv or '--multi' in sys.argv
    speeds = [0, 20, 25, 30, 35] if multi_speed else [0]

    for arg in sys.argv[1:]:
        if arg.startswith('--chunk-minutes='):
            chunk_minutes = int(arg.split('=')[1])
        if arg.startswith('--threshold='):
            threshold_db = int(arg.split('=')[1])

    print(f"=== UHSDR CWT Evaluation ===")
    print(f"Recording: {RECORDING} (minutes 15-30)")
    print(f"Answer key: {len(ANSWER_KEY)} callsigns")
    print(f"Signal detection threshold: {threshold_db} dB above noise")
    print(f"Speeds: {['auto' if s==0 else f'{s}wpm' for s in speeds]}")
    print()

    scp = load_scp()
    print(f"Loaded {len(scp)} SCP callsigns")

    # Process in chunks to manage memory
    all_decoded_calls = {}  # call -> [(freq_khz, power_db, text)]
    all_signals_processed = 0

    rate = 192000
    start_min = 15
    end_min = 30

    for chunk_start_min in range(start_min, end_min, chunk_minutes):
        chunk_end_min = min(chunk_start_min + chunk_minutes, end_min)
        chunk_dur = (chunk_end_min - chunk_start_min) * 60

        print(f"\n--- Processing minutes {chunk_start_min}-{chunk_end_min} ---")
        print(f"Loading {chunk_dur}s of 24-bit IQ...", end=" ", flush=True)

        iq = read_24bit_iq_chunk(RECORDING, chunk_start_min * 60, chunk_dur, rate)
        print(f"{len(iq)} samples loaded")

        # Find signals
        print("Finding CW signals...", end=" ", flush=True)
        signals = find_cw_signals(iq, rate, threshold_db=threshold_db)
        print(f"{len(signals)} signals found")

        # Pre-compute audio for each signal (SSB demod + decimate once, decode at multiple speeds)
        for i, (freq_hz, power_db) in enumerate(signals):
            freq_khz = CENTER_FREQ_KHZ + freq_hz / 1000
            all_signals_processed += 1

            # SSB demod + decimate (do once per signal)
            n = len(iq)
            t = np.arange(n) / rate
            mix_freq = freq_hz - CW_TONE
            mixed = iq * np.exp(-1j * 2 * np.pi * mix_freq * t)
            audio = mixed.real
            audio = scipy_decimate(audio, 16, ftype='fir', n=63)
            peak = np.max(np.abs(audio))
            if peak > 0:
                audio = audio / peak * 16000
            audio_pcm = audio.astype(np.int16).tobytes()

            # Run decoder at each speed
            for wpm in speeds:
                cmd = [UHSDR_BIN, "-r", str(OUTPUT_RATE), "-f", str(CW_TONE)]
                if wpm > 0:
                    cmd += ["-s", str(wpm)]
                proc = subprocess.run(cmd, input=audio_pcm, capture_output=True, timeout=60)
                out = proc.stdout.decode('utf-8', errors='replace').strip()
                lines = [l for l in out.split('\n') if not l.startswith('uhsdr_cw')]
                text = ' '.join(lines).strip()

                # Extract callsigns
                calls = extract_callsigns(text)
                for call in calls:
                    if call in scp or call in ANSWER_KEY:
                        if call not in all_decoded_calls:
                            all_decoded_calls[call] = []
                        all_decoded_calls[call].append((freq_khz, power_db, text[:80]))

            # Progress
            if (i + 1) % 20 == 0 or i == len(signals) - 1:
                hits_so_far = len([c for c in all_decoded_calls if c in ANSWER_KEY])
                print(f"  Signal {i+1}/{len(signals)}: {freq_khz:.1f} kHz ({power_db:.0f} dB) — {hits_so_far} answer key hits so far",
                      flush=True)

        # Free memory
        del iq

    # Score against answer key
    print(f"\n{'='*80}")
    print(f"=== RESULTS ===")
    print(f"Total signals processed: {all_signals_processed}")
    print(f"Unique callsigns decoded (in SCP): {len(all_decoded_calls)}")

    hits = [c for c in all_decoded_calls if c in ANSWER_KEY]
    false_pos = [c for c in all_decoded_calls if c not in ANSWER_KEY]

    print(f"\nAnswer key hits: {len(hits)}/{len(ANSWER_KEY)} ({100*len(hits)/len(ANSWER_KEY):.1f}%)")
    print(f"False positives (in SCP but not in answer key): {len(false_pos)}")

    if hits:
        print(f"\nDecoded from answer key:")
        for call in sorted(hits):
            entries = all_decoded_calls[call]
            freqs = ', '.join(f"{f:.1f}kHz" for f, _, _ in entries[:3])
            print(f"  {call:<12} at {freqs}")

    if false_pos:
        print(f"\nFalse positives (sample):")
        for call in sorted(false_pos)[:20]:
            entries = all_decoded_calls[call]
            print(f"  {call:<12} ({len(entries)} sightings)")

    missed = ANSWER_KEY - set(hits)
    print(f"\nMissed ({len(missed)}):")
    missed_sorted = sorted(missed)
    for i in range(0, len(missed_sorted), 10):
        print(f"  {' '.join(missed_sorted[i:i+10])}")

if __name__ == '__main__':
    main()
