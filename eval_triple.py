#!/usr/bin/env python3
"""Score UHSDR + ML + bmorse triple ensemble on CWT."""
import numpy as np, torch, sys, re, os, wave, subprocess, tempfile
sys.path.insert(0, '.')
from train_model import CWDecoder, ctc_greedy_decode, compute_spectrogram
from sparkgap import read_24bit_iq_chunk
from scipy.signal import decimate

ANSWER_KEY = set('9Y4D,AA3B,AA4NP,AA6G,AD4UB,AI5IN,AJ6V,CY0S,DF7TV,EB1EOE,F8NHF,G3LDI,HA7NZ,HA9RE,HZ1TT,I1MMR,IK4QJF,K0AWU,K0CDJ,K0IS,K0JM,K1BZ,K1DW,K1GU,K1HZ,K2AR,K2LE,K3FI,K3JT,K4IU,K5DXR,K5PE,K5TN,K5YC,K5YCM,K6RAD,K8WWS,K9MA,KB2BK,KB4EKK,KD0RC,KD4JG,KE2D,KH6M,KI7MD,KM0O,KM9R,KV0I,KW7Q,M2RQ,M7JET,N2CG,N2EY,N3AD,N3JT,N4GO,N5AW,N5JJ,N5NA,N5XZ,N7DEY,N7UA,N9FZ,ND9M,NJ6Q,NN7M,NQ5P,NT5V,NT6Q,NY6C,OH5RF,OM2XW,ON4TH,PA3AAV,PY2NA,R6JY,RD3R,RK3Q,S55DX,S5SH,SP7NHS,TG9ADM,UN6ZZI,UR5EN,VE3KIU,VE6JF,VE7WO,VE7ZO,W0EAS,W0PAB,W0TG,W1QK,W1TO,W2GD,W2NMI,W3US,W4CMG,W4IT,W4SPR,W5JMW,W5RY,W5TM,W6AJR,W6IWI,W7JET,W7MTL,W8EH,W8XAL,W9CF,W9ILY,WA0I,WA0T,WA5RML,WB0OQV,WB2AA,WR7T,WU6P,ZA1EM'.split(','))
CALL_RE = re.compile(r'[A-Z0-9]{1,3}\d{1,4}[A-Z]{1,4}')
FALSE_POS = {'CQ','TEST','QRZ','DE','TU','5NN','599'}
BMORSE = '/home/fred/morse-wip/src/bmorse'

scp = set()
with open('COMBINED.SCP') as f:
    for line in f:
        l = line.strip().upper()
        if l and not l.startswith('#'): scp.add(l)

start_min = int(sys.argv[1]) if len(sys.argv) > 1 else 15
end_min = int(sys.argv[2]) if len(sys.argv) > 2 else 30
dur_sec = (end_min - start_min) * 60
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
            # CW band limits: 7000-7074 and 7077-7125 kHz (skip FT8, skip >7125)
            rf_khz_check = 7090 + freqs_fft[i] / 1000
            if rf_khz_check < 7000 or rf_khz_check > 7125:
                continue
            if 7074 <= rf_khz_check <= 7077:  # FT8 wall
                continue
            key = int(round(freqs_fft[i]/200))*200
            snr = avg_db[i]-noise
            if key not in all_signals or snr > all_signals[key][1]:
                all_signals[key] = (freqs_fft[i], snr)
clustered = sorted(all_signals.values())
print(f"{len(clustered)} signals detected", flush=True)

# Load ML model
model = CWDecoder()
ckpt = torch.load('cw_decoder_ctc_best.pth', map_location='cpu')
model.load_state_dict(ckpt['model_state_dict'])
model.eval()

# Auto pitch detection
def detect_pitch(audio, sr, pitches=[500,550,600,650,700,750,800]):
    n = min(len(audio), sr*2)
    spectrum = np.abs(np.fft.rfft(audio[:n]*np.hanning(n)))
    f = np.fft.rfftfreq(n, 1.0/sr)
    mask = (f >= 475) & (f <= 825)
    if not np.any(mask): return 600
    return min(pitches, key=lambda p: abs(p - f[mask][np.argmax(spectrum[mask])]))

t = np.arange(len(iq))/rate
uhsdr_calls, ml_calls, bmorse_calls = {}, {}, {}
tmpwav = '/tmp/bmorse_eval.wav'

for si, (freq_hz, snr) in enumerate(clustered):
    rf_khz = 7090 + freq_hz/1000

    # Channelize
    mixed = iq * np.exp(-1j*2*np.pi*(freq_hz-600)*t)
    audio = mixed.real
    audio_12k = decimate(audio, 16, ftype='fir', n=63)
    actual_pitch = detect_pitch(audio_12k, 12000)
    if actual_pitch != 600:
        mixed = iq * np.exp(-1j*2*np.pi*(freq_hz-actual_pitch)*t)
        audio = mixed.real
        audio_12k = decimate(audio, 16, ftype='fir', n=63)

    # UHSDR
    pcm = np.clip(audio_12k*0.2, -32000, 32000).astype(np.int16).tobytes()
    proc = subprocess.run(['./uhsdr_cw','-r','12000','-f',str(actual_pitch)],
                          input=pcm, capture_output=True, timeout=60)
    text = proc.stdout.decode('utf-8', errors='replace').upper()
    for m in CALL_RE.finditer(text):
        c = m.group(0)
        if len(c)>=4 and c not in FALSE_POS and c in scp and c not in uhsdr_calls:
            uhsdr_calls[c] = rf_khz

    # ML
    audio_4k = decimate(audio, 48, ftype='fir', n=127)
    peak = np.max(np.abs(audio_4k))
    if peak > 0: audio_4k = audio_4k/peak*0.8
    spec = compute_spectrogram(audio_4k.astype(np.float32), fft_size=128, hop=32)
    all_text = []
    for start in range(0, max(1, spec.shape[0]-384), 384):
        chunk = spec[start:start+768]
        if chunk.shape[0] < 768:
            chunk = np.pad(chunk, ((0,768-chunk.shape[0]),(0,0)))
        tensor = torch.tensor(chunk).unsqueeze(0).unsqueeze(0)
        with torch.no_grad():
            output = model(tensor)
            all_text.append(ctc_greedy_decode(output[0].cpu()))
    ml_text = ' '.join(all_text).upper()
    for m in CALL_RE.finditer(ml_text):
        c = m.group(0)
        if len(c)>=4 and c not in FALSE_POS and c in scp and c not in ml_calls:
            ml_calls[c] = rf_khz

    # bmorse (batch via temp WAV)
    w = wave.open(tmpwav, 'w')
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(4000)
    w.writeframes((audio_4k*32767).astype(np.int16).tobytes())
    w.close()
    proc = subprocess.run([BMORSE,'-txt','-agc','-spd','30','-frq',str(int(actual_pitch)),tmpwav],
                          capture_output=True, timeout=60)
    btext = proc.stdout.decode('utf-8', errors='replace').upper()
    for m in CALL_RE.finditer(btext):
        c = m.group(0)
        if len(c)>=4 and c not in FALSE_POS and c in scp and c not in bmorse_calls:
            bmorse_calls[c] = rf_khz

    if (si+1) % 20 == 0:
        u = len(set(uhsdr_calls)&ANSWER_KEY)
        m = len(set(ml_calls)&ANSWER_KEY)
        b = len(set(bmorse_calls)&ANSWER_KEY)
        triple = len((set(uhsdr_calls)|set(ml_calls)|set(bmorse_calls))&ANSWER_KEY)
        print(f"  Signal {si+1}/{len(clustered)}: {rf_khz:.1f} kHz — U={u} M={m} B={b} triple={triple}", flush=True)

os.remove(tmpwav)
u_hits = set(uhsdr_calls)&ANSWER_KEY
m_hits = set(ml_calls)&ANSWER_KEY
b_hits = set(bmorse_calls)&ANSWER_KEY
triple = u_hits|m_hits|b_hits
print(f"\n{'='*60}")
print(f"UHSDR:     {len(u_hits)}/118")
print(f"ML:        {len(m_hits)}/118")
print(f"bmorse:    {len(b_hits)}/118")
print(f"TRIPLE:    {len(triple)}/118")
print(f"bmorse unique: {sorted(b_hits - u_hits - m_hits)}")
print(f"ML unique:     {sorted(m_hits - u_hits - b_hits)}")
print(f"UHSDR unique:  {sorted(u_hits - m_hits - b_hits)}")
