#!/usr/bin/env python3
"""
extract_signal.py — Extract a single CW signal from IQ WAV, mix to baseband,
downsample to 4kHz, and run through bmorse Bayesian decoder.

Usage: python3 extract_signal.py <input.wav> <freq_hz> [sample_rate]
"""
import wave, struct, math, sys, subprocess, tempfile, os

def main():
    if len(sys.argv) < 3:
        print("Usage: extract_signal.py <input.wav> <freq_hz> [sample_rate]")
        return

    infile = sys.argv[1]
    target_freq = int(sys.argv[2])

    # Read input
    w = wave.open(infile, 'rb')
    sr = w.getframerate()
    nch = w.getnchannels()
    data = w.readframes(w.getnframes())
    w.close()

    samples = struct.unpack('<' + 'h' * (len(data)//2), data)

    # Get mono (I channel if stereo)
    if nch == 2:
        mono = list(samples[0::2])
    else:
        mono = list(samples)

    # Mix signal from target_freq to 600 Hz (bmorse default center)
    # Then downsample to 4kHz
    out_sr = 4000
    target_audio = 600  # bmorse expects tone at 600 Hz
    decimation = sr // out_sr

    # Mix: shift target_freq to 600 Hz
    shift_freq = target_freq - target_audio
    baseband = []
    phase = 0.0
    omega = 2.0 * math.pi * shift_freq / sr

    buf = 0.0
    count = 0

    for s in mono:
        # Frequency shift — multiply by cos to move signal
        mixed = s * math.cos(phase) * 2.0  # x2 for amplitude preservation
        phase += omega
        if phase > 2 * math.pi:
            phase -= 2 * math.pi

        # Accumulate for decimation (simple averaging = low-pass)
        buf += mixed
        count += 1

        if count >= decimation:
            avg = buf / count
            baseband.append(int(max(min(avg, 32767), -32768)))
            buf = 0.0
            count = 0

    # Write as 4kHz WAV
    tmpfile = tempfile.mktemp(suffix='.wav')
    out = wave.open(tmpfile, 'wb')
    out.setnchannels(1)
    out.setsampwidth(2)
    out.setframerate(out_sr)
    out.writeframes(struct.pack('<' + 'h' * len(baseband), *baseband))
    out.close()

    # Run bmorse
    try:
        result = subprocess.run(
            ['/home/fred/morse-wip/src/bmorse', '-txt', '-frq', '600', '-spd', '25', tmpfile],
            capture_output=True, text=True, timeout=30
        )
        decoded = result.stdout.strip()
        if decoded and len(decoded) > 2:
            print(f"{target_freq}:25:{decoded}")
    except:
        pass
    finally:
        os.unlink(tmpfile)

if __name__ == '__main__':
    main()
