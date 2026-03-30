#!/usr/bin/env python3
"""Score UHSDR + ML + bmorse(DSP-WPM) + HamFist quad ensemble on CWT.
v6: Uses DSP WPM estimator instead of ML WPM head for bmorse seeding."""
import numpy as np, torch, sys, re, os, subprocess
sys.path.insert(0, '.')
from train_model import CWDecoder, ctc_greedy_decode, compute_spectrogram
from openskimmer import read_24bit_iq_chunk
from wpm_estimator import estimate_wpm_fast

ANSWER_KEY = set('9Y4D,AA3B,AA4NP,AA6G,AD4UB,AI5IN,AJ6V,CY0S,DF7TV,EB1EOE,F8NHF,G3LDI,HA7NZ,HA9RE,HZ1TT,I1MMR,IK4QJF,K0AWU,K0CDJ,K0IS,K0JM,K1BZ,K1DW,K1GU,K1HZ,K2AR,K2LE,K3FI,K3JT,K4IU,K5DXR,K5PE,K5TN,K5YC,K5YCM,K6RAD,K8WWS,K9MA,KB2BK,KB4EKK,KD0RC,KD4JG,KE2D,KH6M,KI7MD,KM0O,KM9R,KV0I,KW7Q,M2RQ,M7JET,N2CG,N2EY,N3AD,N3JT,N4GO,N5AW,N5JJ,N5NA,N5XZ,N7DEY,N7UA,N9FZ,ND9M,NJ6Q,NN7M,NQ5P,NT5V,NT6Q,NY6C,OH5RF,OM2XW,ON4TH,PA3AAV,PY2NA,R6JY,RD3R,RK3Q,S55DX,S5SH,SP7NHS,TG9ADM,UN6ZZI,UR5EN,VE3KIU,VE6JF,VE7WO,VE7ZO,W0EAS,W0PAB,W0TG,W1QK,W1TO,W2GD,W2NMI,W3US,W4CMG,W4IT,W4SPR,W5JMW,W5RY,W5TM,W6AJR,W6IWI,W7JET,W7MTL,W8EH,W8XAL,W9CF,W9ILY,WA0I,WA0T,WA5RML,WB0OQV,WB2AA,WR7T,WU6P,ZA1EM'.split(','))
CALL_RE = re.compile(r'[A-Z0-9]{1,3}\d{1,4}[A-Z]{1,4}')
FALSE_POS = {'CQ','TEST','QRZ','DE','TU','5NN','599'}
BMORSE    = '/home/fred/morse-wip/src/bmorse'
HAMFIST   = '/home/fred/csdr-skimmer/research/HamFist/hamfist'
SCP_FILE  = '/home/fred/csdr-skimmer/COMBINED.SCP'

scp = set()
with open(SCP_FILE) as f:
    for line in f:
        l = line.strip().upper()
        if l and not l.startswith('#'): scp.add(l)

start_min = int(sys.argv[1]) if len(sys.argv) > 1 else 15
end_min   = int(sys.argv[2]) if len(sys.argv) > 2 else 30
dur_sec   = (end_min - start_min) * 60
print(f"Loading {start_min}-{end_min} min...", flush=True)
i_ch, q_ch = read_24bit_iq_chunk('B1_20260319_030000_7090kHz.wav', start_min*60, dur_sec)
rate = 192000
iq = i_ch + 1j * q_ch

# Full-duration signal scan
fft_size = 8192
freqs_fft = np.fft.fftfreq(fft_size, 1.0/rate)
all_signals = {}
window_samples = 60 * rate
for win_start in range(0, len(i_ch), window_samples):
    win_iq = iq[win_start:win_start+window_samples]
    n_ffts = min(len(win_iq)//fft_size, 500)
    if n_ffts < 10: continue
    avg = np.zeros(fft_size)
    for fi in range(n_ffts):
        chunk = win_iq[fi*fft_size:(fi+1)*fft_size]
        avg += np.abs(np.fft.fft(chunk*np.hanning(fft_size)))**2
    avg /= n_ffts
    avg_db = 10*np.log10(avg+1e-20)
    noise = np.median(avg_db)
    for i in range(1, fft_size-1):
        if avg_db[i] > noise+5 and avg_db[i] > avg_db[i-1] and avg_db[i] > avg_db[i+1]:
            rf_khz_check = 7090 + freqs_fft[i] / 1000
            if rf_khz_check < 7000 or rf_khz_check > 7125: continue
            if 7074 <= rf_khz_check <= 7077: continue
            key = int(round(freqs_fft[i]/200))*200
            snr = avg_db[i]-noise
            if key not in all_signals or snr > all_signals[key][1]:
                all_signals[key] = (freqs_fft[i], snr)
clustered = sorted(all_signals.values())
print(f"{len(clustered)} signals detected", flush=True)

# Load WPM-tuned ML model (Arc's checkpoint, strict=False for wpm_head)
ML_CKPT = 'cw_decoder_ctc_wpm.pth'
model = CWDecoder()
ckpt = torch.load(ML_CKPT, map_location='cpu', weights_only=True)
model.load_state_dict(ckpt['model_state_dict'], strict=False)
model.eval()
print(f"ML model loaded ({ML_CKPT})", flush=True)

# ML window: first WPM_SEC seconds of 4kHz audio for speed estimation + CALL extraction.
# Short window keeps ML fast (~4 chunks); WPM converges in seconds.
WPM_SEC = 15
WPM_SAMPLES = WPM_SEC * 4000  # samples at 4kHz

def ml_infer(audio_4k):
    """Run ML on first WPM_SEC of audio. Returns (wpm_estimate, set of calls).

    Confidence gate: if CTC produces fewer than MIN_CHARS decoded characters
    the signal is noise-dominated and the WPM head is unreliable — fall back
    to 30 WPM so bmorse/HamFist aren't seeded at a garbage speed.
    """
    MIN_CHARS = 4  # minimum CTC output chars to trust the WPM estimate
    clip = audio_4k[:WPM_SAMPLES].astype(np.float32)
    spec = compute_spectrogram(clip, fft_size=128, hop=32)
    if spec.shape[0] < 32:
        return 30, set()
    wpm_vals, calls, total_chars = [], set(), 0
    for start in range(0, max(1, spec.shape[0] - 384), 384):
        chunk = spec[start:start + 768]
        if chunk.shape[0] < 768:
            chunk = np.pad(chunk, ((0, 768 - chunk.shape[0]), (0, 0)))
        tensor = torch.tensor(chunk).unsqueeze(0).unsqueeze(0)
        with torch.no_grad():
            ctc_out, wpm_t = model(tensor)
            wpm_vals.append(float(wpm_t[0]))
            text = ctc_greedy_decode(ctc_out[0].cpu()).upper()
            total_chars += len(text.replace(' ', ''))
            for m in CALL_RE.finditer(text):
                c = m.group(0)
                if len(c) >= 4 and c not in FALSE_POS and c in scp:
                    calls.add(c)
    if total_chars < MIN_CHARS:
        return 30, calls  # noise — don't trust WPM head
    wpm = int(round(float(np.median(wpm_vals)))) if wpm_vals else 30
    wpm = max(10, min(50, wpm))  # clamp to contest range
    return wpm, calls

def detect_pitch(audio, sr, pitches=[500,550,600,650,700,750,800]):
    n = min(len(audio), sr*2)
    spectrum = np.abs(np.fft.rfft(audio[:n]*np.hanning(n)))
    f = np.fft.rfftfreq(n, 1.0/sr)
    mask = (f >= 475) & (f <= 825)
    if not np.any(mask): return 600
    return min(pitches, key=lambda p: abs(p - f[mask][np.argmax(spectrum[mask])]))

def calls_from_text(text):
    calls = set()
    for m in CALL_RE.finditer(text.upper()):
        c = m.group(0)
        if len(c) >= 4 and c not in FALSE_POS and c in scp:
            calls.add(c)
    return calls

from scipy.signal import decimate as scipy_decimate

# Pre-process ONCE: shift IQ center to 7037 kHz (-53 kHz from 7090), decimate 192k→48k.
# Per-signal work then runs on 43.2M samples instead of 172.8M (4x speedup).
# CW band (7000-7074 kHz) is ±37 kHz from 7037 → fits within ±24 kHz Nyquist of 48kHz.
PRE_SHIFT = -53000  # Hz: re-center on 7037 kHz
t_full = np.arange(len(i_ch)) / rate
iq_full = (i_ch + 1j*q_ch) * np.exp(-1j*2*np.pi*PRE_SHIFT*t_full)
del t_full
iq_96k = (scipy_decimate(iq_full.real, 2, ftype='fir', n=31)
        + 1j*scipy_decimate(iq_full.imag, 2, ftype='fir', n=31))
del iq_full
iq_48k = (scipy_decimate(iq_96k.real, 2, ftype='fir', n=31)
        + 1j*scipy_decimate(iq_96k.imag, 2, ftype='fir', n=31))
del iq_96k
PRE_RATE = 48000
t_48k = np.arange(len(iq_48k)) / PRE_RATE
print(f"Pre-decimated to {PRE_RATE} Hz ({len(iq_48k)} samples)", flush=True)

uhsdr_calls, ml_calls, bmorse_calls, hamfist_calls = {}, {}, {}, {}

for si, (freq_hz, snr) in enumerate(clustered):
    rf_khz = 7090 + freq_hz/1000

    # Per-signal channelization at 48kHz — 4x faster than full 192kHz
    new_freq = freq_hz - PRE_SHIFT  # offset from 7037 kHz pre-decimated center
    mixed_48k = (iq_48k * np.exp(-1j*2*np.pi*(new_freq - 600)*t_48k)).real

    audio_12k = scipy_decimate(mixed_48k, 4,  ftype='fir', n=63)
    actual_pitch = detect_pitch(audio_12k, 12000)

    if actual_pitch != 600:
        mixed_48k = (iq_48k * np.exp(-1j*2*np.pi*(new_freq - actual_pitch)*t_48k)).real
        audio_12k = scipy_decimate(mixed_48k, 4, ftype='fir', n=63)

    audio_4k = scipy_decimate(mixed_48k, 12, ftype='fir', n=127)

    # Peak-normalize
    for arr in (audio_12k, audio_4k):
        pk = np.max(np.abs(arr))
        if pk > 0:
            arr /= pk

    pcm    = np.clip(audio_12k * 0.3 * 32767, -32767, 32767).astype(np.int16).tobytes()
    pcm_4k = np.clip(audio_4k  * 0.3 * 32767, -32767, 32767).astype(np.int16).tobytes()

    # uhsdr_cw (12kHz)
    proc = subprocess.run(['./uhsdr_cw','-r','12000','-f',str(actual_pitch)],
                          input=pcm, capture_output=True, timeout=300)
    for c in calls_from_text(proc.stdout.decode('utf-8', errors='replace')):
        if c not in uhsdr_calls: uhsdr_calls[c] = rf_khz

    # ML inference (short window) — CALL extraction only (WPM from DSP now)
    audio_4k = np.frombuffer(pcm_4k, dtype=np.int16).astype(np.float32) / 32767.0
    _, ml_found = ml_infer(audio_4k)
    for c in ml_found:
        if c not in ml_calls: ml_calls[c] = rf_khz

    # DSP WPM estimation on 12kHz audio (better envelope at wider bandwidth)
    audio_12k_f = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32767.0
    dsp_wpm, dsp_conf = estimate_wpm_fast(audio_12k_f, sample_rate=12000,
                                           tone_freq=actual_pitch, window_sec=10.0)
    wpm = dsp_wpm if dsp_conf > 0.3 and dsp_wpm > 0 else 25

    # bmorse — seeded at DSP-estimated WPM
    proc = subprocess.run([BMORSE,'-stdin','-txt','-agc',
                           '-spd', str(wpm), '-frq',str(int(actual_pitch)),'-rate','4000'],
                          input=pcm_4k, capture_output=True, timeout=300)
    for c in calls_from_text(proc.stdout.decode('utf-8', errors='replace')):
        if c not in bmorse_calls: bmorse_calls[c] = rf_khz

    # HamFist
    proc = subprocess.run([HAMFIST, '-stdin',
                           '-frq', str(int(actual_pitch)),
                           '-rate', '4000',
                           '-spd', '30',
                           '-spd', str(wpm),
                           '-scp', SCP_FILE],
                          input=pcm_4k, capture_output=True, timeout=300)
    htext = proc.stdout.decode('utf-8', errors='replace').upper()
    # HamFist outputs CALL:CALLSIGN lines — extract those directly
    for line in htext.splitlines():
        if line.startswith('CALL:'):
            c = line[5:].strip()
            if c and c in scp and c not in FALSE_POS:
                if c not in hamfist_calls: hamfist_calls[c] = rf_khz

    if (si+1) % 20 == 0:
        u = len(set(uhsdr_calls)&ANSWER_KEY)
        m = len(set(ml_calls)&ANSWER_KEY)
        b = len(set(bmorse_calls)&ANSWER_KEY)
        h = len(set(hamfist_calls)&ANSWER_KEY)
        quad = len((set(uhsdr_calls)|set(ml_calls)|set(bmorse_calls)|set(hamfist_calls))&ANSWER_KEY)
        print(f"  Signal {si+1}/{len(clustered)}: {rf_khz:.1f} kHz — U={u} M={m} B={b} H={h} quad={quad} (dsp_wpm={wpm} conf={dsp_conf:.2f})", flush=True)

u_hits = set(uhsdr_calls)   & ANSWER_KEY
m_hits = set(ml_calls)      & ANSWER_KEY
b_hits = set(bmorse_calls)  & ANSWER_KEY
h_hits = set(hamfist_calls) & ANSWER_KEY
quad   = u_hits | m_hits | b_hits | h_hits

print(f"\n{'='*60}")
print(f"uhsdr:    {len(u_hits)}/118")
print(f"ML:       {len(m_hits)}/118  (15s window)")
print(f"bmorse:   {len(b_hits)}/118  (DSP-WPM-seeded)")
print(f"HamFist:  {len(h_hits)}/118  (WPM-seeded)")
print(f"QUAD:     {len(quad)}/118")
print(f"HamFist unique: {sorted(h_hits - u_hits - m_hits - b_hits)}")
print(f"bmorse unique:  {sorted(b_hits - u_hits - m_hits)}")
print(f"ML unique:      {sorted(m_hits - u_hits - b_hits)}")
print(f"UHSDR unique:   {sorted(u_hits - m_hits - b_hits)}")
