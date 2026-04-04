#!/usr/bin/env python3
"""
eval_model.py — Evaluate CTC model against real contest recordings.

Proper channelization: mix-to-baseband + FIR lowpass + decimate → 4kHz
per-channel audio matching training data format, then run ML model.

Usage: python3 eval_model.py <recording.wav> [--bandwidth 100] [--model cw_decoder_ctc_best.pth]
"""

import argparse
import os
import re
import sys
import wave
import struct
import numpy as np
from scipy.signal import firwin, lfilter, decimate, resample_poly

import torch
from scipy.signal import butter, filtfilt
from train_model import CWDecoder, ctc_greedy_decode, compute_spectrogram, NUM_CLASSES
from beam_decode import (CallsignTrie, ctc_beam_search, ctc_beam_search_constrained,
                         ctc_beam_search_nbest)


CALLSIGN_RE = re.compile(r'\b([A-Z]{1,2}[0-9]{1,2}[A-Z]{1,3}(?:/[A-Z0-9]+)?)\b')
SKIP_WORDS = {'CQ', 'TEST', 'QRZ', 'DE', 'TU', 'RST', 'AGN', 'BK', 'UR', 'QSL',
              'QTH', 'QRL', 'CFM', 'PSE', 'TNX', 'TKS', 'HW', 'CPY', 'BT'}


def load_master_scp(path='MASTER.SCP'):
    calls = set()
    if not os.path.exists(path):
        print(f"Warning: {path} not found", file=sys.stderr)
        return calls
    with open(path) as f:
        for line in f:
            line = line.strip().upper()
            if line and not line.startswith('#'):
                calls.add(line)
    print(f"Loaded {len(calls)} callsigns from {path}", file=sys.stderr)
    return calls


def extract_callsigns(text):
    calls = set()
    for m in CALLSIGN_RE.finditer(text.upper()):
        call = m.group(1)
        if call not in SKIP_WORDS and len(call) >= 4:
            calls.add(call)
    return calls


def find_active_channels(samples, sample_rate, bandwidth):
    """Find channels with CW signals using power spectral density."""
    fft_size = sample_rate // bandwidth
    is_complex = np.iscomplexobj(samples)
    window = np.hanning(fft_size)

    # Average power over multiple frames
    n_frames = min(20, len(samples) // fft_size)
    if is_complex:
        power = np.zeros(fft_size)
        for i in range(n_frames):
            frame = samples[i * fft_size:(i + 1) * fft_size] * window
            power += np.abs(np.fft.fft(frame)) ** 2
        power = power[:fft_size // 2]  # positive freqs only
    else:
        power = np.zeros(fft_size // 2 + 1)
        for i in range(n_frames):
            frame = samples[i * fft_size:(i + 1) * fft_size].real * window
            power += np.abs(np.fft.rfft(frame)) ** 2
    power /= n_frames

    # Find peaks above noise floor
    noise_floor = np.median(power)
    threshold = noise_floor * 6.0  # CW signals should be well above noise
    active = np.where(power > threshold)[0]

    # Convert bin indices to center frequencies
    channels = []
    for bin_idx in active:
        freq = bin_idx * bandwidth
        if freq > 200 and freq < sample_rate // 2 - 200:  # skip edges
            channels.append((freq, 10 * np.log10(power[bin_idx] / max(noise_floor, 1e-10))))

    return channels


def channelize(samples, sample_rate, center_freq, target_rate=4000, cw_pitch=600.0):
    """Extract a single channel: mix to baseband, polyphase decimate.

    Uses resample_poly (polyphase FIR) instead of lfilter — ~10x faster for
    high decimation ratios (192kHz→4kHz = 48x). Float32 chunked mixing to
    avoid float64 OOM on long recordings.
    """
    n = len(samples)
    mix_freq = float(center_freq - cw_pitch)
    decim_factor = int(sample_rate) // int(target_rate)

    # Mix in float32 chunks
    chunk_size = 1 << 22  # 4M samples per chunk (~32 MB float32)
    mixed = np.empty(n, dtype=np.float32)
    phase_inc = -2.0 * np.pi * mix_freq / sample_rate
    for start in range(0, n, chunk_size):
        end = min(start + chunk_size, n)
        idx = np.arange(start, end, dtype=np.float32)
        phases = (phase_inc * idx).astype(np.float32)
        if np.iscomplexobj(samples):
            lo_r = np.cos(phases)
            lo_i = np.sin(phases)
            mixed[start:end] = (samples[start:end].real * lo_r
                                - samples[start:end].imag * lo_i) * 2.0
        else:
            mixed[start:end] = samples[start:end].real * np.cos(phases) * 2.0

    # Polyphase decimate: internally uses a short anti-aliasing FIR, O(n) not O(n*taps)
    decimated = resample_poly(mixed, 1, decim_factor).astype(np.float32)
    del mixed

    peak = np.max(np.abs(decimated))
    if peak > 1e-6:
        decimated /= peak

    return decimated


def demodulate_envelope(audio, sample_rate=4000, cw_pitch=600.0, lowpass_hz=25.0):
    """AG1LE-style AM demodulation: extract CW keying envelope.

    Takes channelized audio with CW tone at cw_pitch Hz, AM demodulates
    to extract the on/off keying envelope, lowpass filters, and decimates.

    Output: clean envelope signal at ~125 Hz sample rate.
    The neural network sees mark/space transitions instead of raw audio.
    """
    t = np.arange(len(audio), dtype=np.float64) / sample_rate

    # AM demodulate: mix with tone, take magnitude
    mixed = audio * ((1 + np.sin(2 * np.pi * cw_pitch * t)) / 2)
    envelope = np.abs(mixed)

    # Butterworth lowpass at 25 Hz (AG1LE's compromise: 20-40 Hz range)
    wn = lowpass_hz / (sample_rate / 2.0)
    if wn >= 1.0:
        wn = 0.99  # clamp for very low sample rates
    b, a = butter(3, wn)
    filtered = filtfilt(b, a, envelope)

    # Decimate to ~125 Hz (8ms per sample)
    decim_factor = max(1, int(sample_rate / 125))
    decimated = filtered[::decim_factor]

    # Normalize to [0, 1]
    peak = np.max(decimated)
    if peak > 1e-6:
        decimated = decimated / peak

    return decimated.astype(np.float32)


def run_model_on_channel(channel_audio, model, device, trie=None, use_beam=True, beam_width=10, use_demod=False):
    """Run the CTC model on a single channel's audio.

    If use_demod=True, applies AG1LE-style AM demodulation to extract the
    keying envelope before computing the spectrogram. The model sees
    mark/space transitions instead of raw modulated audio.
    """
    if use_demod:
        channel_audio = demodulate_envelope(channel_audio)
    spec = compute_spectrogram(channel_audio, fft_size=128, hop=32)
    if spec.shape[0] < 32:
        return ""

    window_size = 768
    hop_frames = 384
    all_text = []

    for start in range(0, max(1, spec.shape[0] - window_size // 2), hop_frames):
        chunk = spec[start:start + window_size]
        if chunk.shape[0] < window_size:
            chunk = np.pad(chunk, ((0, window_size - chunk.shape[0]), (0, 0)))

        tensor = torch.tensor(chunk).unsqueeze(0).unsqueeze(0).to(device)
        with torch.no_grad():
            output = model(tensor)

            # Always run greedy (output is tuple (ctc, wpm); squeeze batch dim)
            greedy = ctc_greedy_decode(output[0][0].cpu())
            if greedy and len(greedy.strip()) >= 2:
                all_text.append(greedy.strip())

            # Also run beam search for additional candidates
            if use_beam and trie is not None:
                candidates = ctc_beam_search_nbest(
                    output[0][0].cpu(), trie, beam_width=beam_width,
                    n_best=5, lm_weight=0.0, callsign_bonus=0.0)
                for cand_text, _ in candidates:
                    cand = cand_text.strip()
                    if cand and len(cand) >= 2 and cand != greedy.strip():
                        all_text.append(cand)

    return ' | '.join(all_text)


def load_gold_standard(path='cwskimmer_spots.txt'):
    calls = set()
    if not os.path.exists(path):
        return calls
    with open(path) as f:
        for line in f:
            for m in CALLSIGN_RE.finditer(line.upper()):
                call = m.group(1)
                if call not in SKIP_WORDS and len(call) >= 4:
                    calls.add(call)
    return calls


def main():
    parser = argparse.ArgumentParser(description='Evaluate CTC model on real recordings')
    parser.add_argument('wav', help='Input WAV recording')
    parser.add_argument('--sample-rate', type=int, default=0, help='Override sample rate')
    parser.add_argument('--model', default='cw_decoder_ctc_best.pth', help='Model checkpoint')
    parser.add_argument('--bandwidth', type=int, default=100, help='Channel bandwidth Hz')
    parser.add_argument('--gold', default='cwskimmer_spots.txt', help='CW Skimmer reference')
    parser.add_argument('--dump-channels', action='store_true', help='Save per-channel WAVs')
    parser.add_argument('--no-beam', action='store_true', help='Use greedy decode instead of beam search')
    parser.add_argument('--beam-width', type=int, default=50, help='Beam width for beam search')
    parser.add_argument('--demod', action='store_true', help='AG1LE-style AM demodulation before spectrogram')
    parser.add_argument('--max-duration', type=float, default=900, help='Max seconds to load (default 900)')
    args = parser.parse_args()

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Device: {device}", file=sys.stderr)

    # Load callsign trie for beam search
    use_beam = not args.no_beam
    trie = None
    if use_beam:
        trie = CallsignTrie.from_file('MASTER.SCP')
        print(f"Beam search: width={args.beam_width}, trie={trie.size} callsigns", file=sys.stderr)
    else:
        print("Using greedy decode", file=sys.stderr)

    # Load model
    print(f"Loading model: {args.model}", file=sys.stderr)
    model = CWDecoder().to(device)
    ckpt = torch.load(args.model, map_location=device, weights_only=True)
    if isinstance(ckpt, dict) and 'model_state_dict' in ckpt:
        model.load_state_dict(ckpt['model_state_dict'], strict=False)
        print(f"  Epoch {ckpt.get('epoch', '?')}, char_acc={ckpt.get('char_acc', 0):.1f}%",
              file=sys.stderr)
    else:
        model.load_state_dict(ckpt)
    model.eval()

    # Load databases
    master_scp = load_master_scp()
    gold_calls = load_gold_standard(args.gold)

    # Read WAV (supports 24-bit WAVEX via soundfile, limits to max_duration to avoid OOM)
    import soundfile as sf
    print(f"Reading: {args.wav}", file=sys.stderr)
    info = sf.info(args.wav)
    sr = info.samplerate if args.sample_rate == 0 else args.sample_rate
    nch = info.channels
    max_frames = int(args.max_duration * sr) if args.max_duration else info.frames
    n_frames = min(info.frames, max_frames)

    with sf.SoundFile(args.wav) as f:
        block = f.read(n_frames, dtype='float32', always_2d=True)

    if nch >= 2:
        i_ch = block[:, 0]
        q_ch = block[:, 1]
        del block
        # Force complex64 (not complex128) — halves memory and speeds mixing
        samples = np.empty(len(i_ch), dtype=np.complex64)
        samples.real[:] = i_ch
        samples.imag[:] = q_ch
        del i_ch, q_ch
        print(f"  Stereo IQ, {len(samples)} samples, {sr} Hz, {len(samples)/sr:.1f}s", file=sys.stderr)
    else:
        samples = block[:, 0].copy()
        del block
        print(f"  Mono, {len(samples)} samples, {sr} Hz, {len(samples)/sr:.1f}s", file=sys.stderr)

    # Find active channels
    print(f"\nScanning for signals ({args.bandwidth} Hz channels)...", file=sys.stderr)
    channels = find_active_channels(samples, sr, args.bandwidth)
    print(f"  Found {len(channels)} active channels", file=sys.stderr)

    # Channelize and decode each
    all_calls = {}  # call -> [(freq, snr, text)]
    dump_dir = '/tmp/cw_channels' if args.dump_channels else None
    if dump_dir:
        os.makedirs(dump_dir, exist_ok=True)

    for idx, (freq, snr_db) in enumerate(channels):
        # Extract channel audio at 4kHz
        channel_audio = channelize(samples, sr, freq, target_rate=4000)

        if args.dump_channels:
            # Save as WAV for inspection
            out_path = os.path.join(dump_dir, f'ch_{freq:06d}Hz.wav')
            wout = wave.open(out_path, 'wb')
            wout.setnchannels(1)
            wout.setsampwidth(2)
            wout.setframerate(4000)
            audio_int = (channel_audio * 32767).astype(np.int16)
            wout.writeframes(audio_int.tobytes())
            wout.close()

        # Run ML model
        decoded = run_model_on_channel(channel_audio, model, device, trie=trie, use_beam=use_beam, beam_width=args.beam_width, use_demod=args.demod)
        if not decoded:
            continue

        # Extract callsigns
        calls = extract_callsigns(decoded)
        if calls or len(decoded) > 5:
            print(f"  {freq:6d} Hz (SNR {snr_db:4.1f} dB): {decoded}", file=sys.stderr)

        for call in calls:
            if call not in all_calls:
                all_calls[call] = []
            all_calls[call].append((freq, snr_db, decoded))

    # Validate
    validated = {c: h for c, h in all_calls.items() if c in master_scp}
    unvalidated = {c: h for c, h in all_calls.items() if c not in master_scp}

    # Multi-sighting bonus: calls seen on multiple frequencies are more likely real
    multi_sight = {c: h for c, h in validated.items() if len(h) >= 2}

    # Results
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"ML MODEL RESULTS", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Active channels scanned: {len(channels)}", file=sys.stderr)
    print(f"Raw callsigns found:     {len(all_calls)}", file=sys.stderr)
    print(f"Validated (in SCP):      {len(validated)}", file=sys.stderr)
    print(f"  Multi-sighting (2+):   {len(multi_sight)}", file=sys.stderr)
    print(f"Unvalidated:             {len(unvalidated)}", file=sys.stderr)

    if gold_calls:
        ml_set = set(validated.keys())
        overlap = ml_set & gold_calls
        ml_only = ml_set - gold_calls
        gold_only = gold_calls - ml_set
        print(f"\nvs CW Skimmer ({len(gold_calls)} calls):", file=sys.stderr)
        print(f"  Both found:            {len(overlap)}", file=sys.stderr)
        print(f"  ML only (new):         {len(ml_only)}", file=sys.stderr)
        print(f"  Gold only (missed):    {len(gold_only)}", file=sys.stderr)

        if overlap:
            print(f"\n  MATCHED calls:", file=sys.stderr)
            for call in sorted(overlap):
                freqs = ', '.join(f"{h[0]}Hz" for h in validated[call])
                print(f"    {call:10s} @ {freqs}", file=sys.stderr)

        if ml_only:
            print(f"\n  ML-ONLY finds:", file=sys.stderr)
            for call in sorted(ml_only):
                freqs = ', '.join(f"{h[0]}Hz" for h in validated[call])
                print(f"    {call:10s} @ {freqs}", file=sys.stderr)

    if unvalidated:
        print(f"\n  Unvalidated (not in SCP):", file=sys.stderr)
        for call in sorted(unvalidated):
            freqs = ', '.join(f"{h[0]}Hz" for h in unvalidated[call])
            print(f"    {call:10s} @ {freqs}", file=sys.stderr)

    # Print validated calls to stdout
    for call in sorted(validated.keys()):
        print(call)

    if dump_dir:
        print(f"\nChannel WAVs saved to {dump_dir}", file=sys.stderr)


if __name__ == '__main__':
    main()
