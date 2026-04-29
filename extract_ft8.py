#!/usr/bin/env python3
"""Extract FT8 sub-band from 192 kHz IQ recording and create .c2 files for ft8d.

Usage:
    python3 extract_ft8.py <iq_wav> [--center-khz 7090] [--ft8-khz 7074] [--start-sec 0]

The recording at center_khz with 192 kHz bandwidth covers the FT8 sub-band.
We mix down to the FT8 frequency, decimate to 4000 sps, and write 60-second
.c2 files that ft8d can decode.
"""
import numpy as np
import struct
import sys
import os

def read_iq_chunk(filename, start_sec, duration_sec, rate=192000):
    """Read 24-bit stereo IQ from WAV."""
    from openskimmer import read_24bit_iq_chunk
    i_arr, q_arr = read_24bit_iq_chunk(filename, start_sec, duration_sec, rate)
    return np.array(i_arr, dtype=np.float64), np.array(q_arr, dtype=np.float64)


def extract_ft8(wav_path, center_khz=7090, ft8_khz=7074, start_sec=0,
                duration_sec=60, output_dir='.'):
    """Extract FT8 sub-band and write .c2 file."""
    rate_in = 192000
    rate_out = 4000
    dec_ratio = rate_in // rate_out  # 48

    # Read IQ
    print(f"Reading {duration_sec}s from {wav_path} at t={start_sec}s...")
    i_data, q_data = read_iq_chunk(wav_path, start_sec, duration_sec, rate_in)
    iq = i_data + 1j * q_data

    # Mix down to FT8 frequency
    offset_hz = (ft8_khz - center_khz) * 1000  # e.g., -16000 Hz
    t = np.arange(len(iq)) / rate_in
    lo = np.exp(-1j * 2 * np.pi * offset_hz * t)
    iq_mixed = iq * lo

    # Low-pass filter before decimation (2 kHz cutoff at 192 kHz)
    from scipy.signal import firwin, lfilter
    taps = firwin(256, 2000.0 / (rate_in / 2))
    iq_filtered = lfilter(taps, 1.0, iq_mixed)

    # Decimate 48:1
    iq_dec = iq_filtered[::dec_ratio]
    n_samples = len(iq_dec)
    print(f"Decimated: {n_samples} samples at {rate_out} sps ({n_samples/rate_out:.1f}s)")

    # Write .c2 file: 8-byte header (dial freq as double) + complex float32
    dial_freq = ft8_khz * 1000.0  # Hz
    # ft8d expects complex samples as interleaved float32 (I, Q, I, Q, ...)
    c2_data = np.zeros(n_samples * 2, dtype=np.float32)
    c2_data[0::2] = iq_dec.real.astype(np.float32)
    c2_data[1::2] = iq_dec.imag.astype(np.float32)

    outfile = os.path.join(output_dir, f"ft8_{ft8_khz}_{start_sec}.c2")
    with open(outfile, 'wb') as f:
        f.write(struct.pack('<d', dial_freq))  # 8-byte header
        f.write(c2_data.tobytes())

    print(f"Wrote {outfile} ({os.path.getsize(outfile)} bytes)")
    return outfile


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('wav', help='IQ WAV file')
    ap.add_argument('--center-khz', type=float, default=7090)
    ap.add_argument('--ft8-khz', type=float, default=7074)
    ap.add_argument('--start-sec', type=float, default=0)
    ap.add_argument('--duration-sec', type=float, default=60)
    args = ap.parse_args()

    c2_file = extract_ft8(args.wav, args.center_khz, args.ft8_khz,
                          args.start_sec, args.duration_sec)

    # Try decoding
    ft8d = '/home/fred/ft8d/ft8d'
    if os.path.exists(ft8d):
        print(f"\nDecoding with ft8d...")
        os.system(f"{ft8d} {c2_file}")
    else:
        print(f"\nRun: {ft8d} {c2_file}")
