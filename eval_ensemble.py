#!/usr/bin/env python3
"""Score UHSDR + ML ensemble on CWT 5-min chunk."""
import numpy as np
import torch
import sys
import re
import os

sys.path.insert(0, '.')
from train_model import CWDecoder, ctc_greedy_decode, compute_spectrogram
from openskimmer import read_24bit_iq_chunk
from scipy.signal import decimate
import subprocess

ANSWER_KEY = set('9Y4D,AA3B,AA4NP,AA6G,AD4UB,AI5IN,AJ6V,CY0S,DF7TV,EB1EOE,F8NHF,G3LDI,HA7NZ,HA9RE,HZ1TT,I1MMR,IK4QJF,K0AWU,K0CDJ,K0IS,K0JM,K1BZ,K1DW,K1GU,K1HZ,K2AR,K2LE,K3FI,K3JT,K4IU,K5DXR,K5PE,K5TN,K5YC,K5YCM,K6RAD,K8WWS,K9MA,KB2BK,KB4EKK,KD0RC,KD4JG,KE2D,KH6M,KI7MD,KM0O,KM9R,KV0I,KW7Q,M2RQ,M7JET,N2CG,N2EY,N3AD,N3JT,N4GO,N5AW,N5JJ,N5NA,N5XZ,N7DEY,N7UA,N9FZ,ND9M,NJ6Q,NN7M,NQ5P,NT5V,NT6Q,NY6C,OH5RF,OM2XW,ON4TH,PA3AAV,PY2NA,R6JY,RD3R,RK3Q,S55DX,S5SH,SP7NHS,TG9ADM,UN6ZZI,UR5EN,VE3KIU,VE6JF,VE7WO,VE7ZO,W0EAS,W0PAB,W0TG,W1QK,W1TO,W2GD,W2NMI,W3US,W4CMG,W4IT,W4SPR,W5JMW,W5RY,W5TM,W6AJR,W6IWI,W7JET,W7MTL,W8EH,W8XAL,W9CF,W9ILY,WA0I,WA0T,WA5RML,WB0OQV,WB2AA,WR7T,WU6P,ZA1EM'.split(','))
CALL_RE = re.compile(r'[A-Z0-9]{1,3}\d{1,4}[A-Z]{1,4}')
FALSE_POS = {'CQ', 'TEST', 'QRZ', 'DE', 'TU', '5NN', '599'}

# Load SCP
scp = set()
with open('COMBINED.SCP') as f:
    for line in f:
        l = line.strip().upper()
        if l and not l.startswith('#'):
            scp.add(l)

# Load IQ
start_min = int(sys.argv[1]) if len(sys.argv) > 1 else 20
end_min = int(sys.argv[2]) if len(sys.argv) > 2 else 25
dur_sec = (end_min - start_min) * 60
print(f"Loading {start_min}-{end_min} min...", flush=True)
i_ch, q_ch = read_24bit_iq_chunk('B1_20260319_030000_7090kHz.wav', start_min * 60, dur_sec)
rate = 192000
iq = i_ch + 1j * q_ch

# Find signals
fft_size = 8192
n_ffts = min(len(i_ch) // fft_size, 200)
avg_spectrum = np.zeros(fft_size)
for fi in range(n_ffts):
    chunk = iq[fi * fft_size:(fi + 1) * fft_size]
    avg_spectrum += np.abs(np.fft.fft(chunk * np.hanning(fft_size))) ** 2
avg_spectrum /= n_ffts
avg_db = 10 * np.log10(avg_spectrum + 1e-20)
freqs = np.fft.fftfreq(fft_size, 1.0 / rate)
noise = np.median(avg_db)

signals = []
for i in range(1, fft_size - 1):
    if avg_db[i] > noise + 8 and avg_db[i] > avg_db[i - 1] and avg_db[i] > avg_db[i + 1]:
        signals.append((freqs[i], avg_db[i] - noise))
clustered = []
for freq, snr in sorted(signals):
    if not clustered or abs(freq - clustered[-1][0]) > 200:
        clustered.append((freq, snr))
    elif snr > clustered[-1][1]:
        clustered[-1] = (freq, snr)

print(f"{len(clustered)} signals detected", flush=True)

# Load ML model
model = CWDecoder()
ckpt = torch.load('cw_decoder_ctc_best.pth', map_location='cpu')
model.load_state_dict(ckpt['model_state_dict'])
model.eval()

t = np.arange(len(iq)) / rate
uhsdr_calls = {}
ml_calls = {}

for si, (freq_hz, snr) in enumerate(clustered):
    rf_khz = 7090 + freq_hz / 1000

    # Channelize
    mixed = iq * np.exp(-1j * 2 * np.pi * (freq_hz - 600) * t)
    audio = mixed.real

    # UHSDR: decimate to 12kHz
    audio_12k = decimate(audio, 16, ftype='fir', n=63)
    peak = np.max(np.abs(audio_12k))
    if peak > 0:
        audio_12k_norm = np.clip(audio_12k * 0.2, -32000, 32000)
    pcm = audio_12k_norm.astype(np.int16).tobytes()
    proc = subprocess.run(['./uhsdr_cw', '-r', '12000', '-f', '600'],
                          input=pcm, capture_output=True, timeout=30)
    uhsdr_text = proc.stdout.decode('utf-8', errors='replace').upper()
    for m in CALL_RE.finditer(uhsdr_text):
        call = m.group(0)
        if len(call) >= 4 and call not in FALSE_POS and call in scp:
            if call not in uhsdr_calls:
                uhsdr_calls[call] = rf_khz

    # ML: decimate to 4kHz
    audio_4k = decimate(audio, 48, ftype='fir', n=127)
    peak = np.max(np.abs(audio_4k))
    if peak > 0:
        audio_4k = audio_4k / peak * 0.8
    spec = compute_spectrogram(audio_4k.astype(np.float32), fft_size=128, hop=32)
    all_text = []
    for start in range(0, max(1, spec.shape[0] - 384), 384):
        chunk = spec[start:start + 768]
        if chunk.shape[0] < 768:
            chunk = np.pad(chunk, ((0, 768 - chunk.shape[0]), (0, 0)))
        tensor = torch.tensor(chunk).unsqueeze(0).unsqueeze(0)
        with torch.no_grad():
            output = model(tensor)
            text = ctc_greedy_decode(output[0].cpu())
            all_text.append(text)
    ml_text = ' '.join(all_text).upper()
    for m in CALL_RE.finditer(ml_text):
        call = m.group(0)
        if len(call) >= 4 and call not in FALSE_POS and call in scp:
            if call not in ml_calls:
                ml_calls[call] = rf_khz

    if (si + 1) % 10 == 0:
        u_hits = set(uhsdr_calls) & ANSWER_KEY
        m_hits = set(ml_calls) & ANSWER_KEY
        combined = u_hits | m_hits
        print(f"  Signal {si + 1}/{len(clustered)}: {rf_khz:.1f} kHz — "
              f"UHSDR={len(u_hits)} ML={len(m_hits)} combined={len(combined)}",
              flush=True)

# Final score
u_hits = set(uhsdr_calls) & ANSWER_KEY
m_hits = set(ml_calls) & ANSWER_KEY
combined = u_hits | m_hits
ml_only = m_hits - u_hits
uhsdr_only = u_hits - m_hits

print(f"\n{'='*60}")
print(f"UHSDR only:    {len(u_hits)}/118 ({len(set(uhsdr_calls) - ANSWER_KEY)} FP)")
print(f"ML only:       {len(m_hits)}/118 ({len(set(ml_calls) - ANSWER_KEY)} FP)")
print(f"ENSEMBLE:      {len(combined)}/118 ({len((set(uhsdr_calls)|set(ml_calls)) - ANSWER_KEY)} FP)")
print(f"ML unique:     {sorted(ml_only)}")
print(f"UHSDR unique:  {sorted(uhsdr_only)}")
print(f"UHSDR hits:    {' '.join(sorted(u_hits))}")
print(f"ML hits:       {' '.join(sorted(m_hits))}")
