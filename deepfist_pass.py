"""DeepFist neural second pass for ITILA scanner bins (issue #5, PR #7).

DeepFist (https://github.com/n9bc/DeepFist, Brent Crier N9BC,
GPL-3.0-or-later) is a CNN+CTC CW decoder. It needs real audio, not the 200 Hz
envelope ITILA decodes (a tone rebuilt from the envelope halves its recall), so
the scanner keeps a 1 kHz complex baseband per bin when capture is enabled
(itila_sc_enable_iq_capture / itila_sc_peek_iq). This module turns a bin's
window of baseband into 3200 Hz audio and runs DeepFist's released ONNX model
(release exp27_bt-champion, deepfist.onnx + deepfist.onnx.json) with
onnxruntime -- no torch, no DeepFist checkout.

Vendored from DeepFist @ 98e7df7 (GPL-3.0-or-later, same as SparkGap), kept
numerically identical to the originals:
  - deepfist/features/conditioner.py  -> _condition()   (AGC, tone AFC, 90 Hz
    matched bandpass, re-centre to 600 Hz, peak-normalise)
  - tools/squelch.py                  -> _keying_ratio() (keying-based squelch)
  - deepfist/features/spectrogram.py  -> _spectrogram()  (torch.stft port to
    numpy: n_fft 256, hop 48, periodic Hann, centre/reflect, 400-1200 Hz,
    log1p, global standardise with unbiased std)
  - deepfist/model/decode.py          -> greedy CTC

Implements the second-pass decoder interface in second_pass.py (registered
as "deepfist"); the background worker, budget and trigger live there and in
sparkgap.py, not here.
"""
import json
import os

import numpy as np
from scipy.signal import lfilter, resample_poly, stft

IQ_RATE = 1000          # scanner capture rate (complex)
SR = 3200               # DeepFist model rate
WIN_SEC = 15.0          # DeepFist eval window

# conditioner (deepfist/features/conditioner.py)
_TONE_NFFT = 4096
_OUT_PITCH = 600.0
_COND_BW_HZ = 90.0
_BAND_LO_HZ = 400.0
_BAND_HI_HZ = 1200.0

# spectrogram (deepfist/features/spectrogram.py)
_N_FFT = 256
_HOP = 48
_LO_BIN = int(np.ceil(_BAND_LO_HZ / (SR / _N_FFT)))
_HI_BIN = int(np.floor(_BAND_HI_HZ / (SR / _N_FFT))) + 1
_HANN = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(_N_FFT) / _N_FFT)).astype(np.float32)  # periodic

# squelch (tools/squelch.py)
_FRAME_MS = 10.0
_CW_LO, _CW_HI = 550, 800
_SQUELCH_THRESH = 12.0


def _detect_tone(x, sr=SR):
    n = min(len(x), _TONE_NFFT)
    if n < 8:
        return _OUT_PITCH
    spec = np.abs(np.fft.rfft(x[:n] * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    idx = np.where((freqs >= _BAND_LO_HZ) & (freqs <= _BAND_HI_HZ))[0]
    if not idx.size:
        return _OUT_PITCH
    return float(freqs[idx[spec[idx].argmax()]])


def _condition(audio, sr=SR):
    x = np.asarray(audio, dtype=np.float32)
    n = len(x)
    if n < _TONE_NFFT:
        return x
    x = x / (np.sqrt((x * x).mean()) + 1e-9)
    tone = _detect_tone(x, sr)
    k = np.arange(n, dtype=np.float64)
    bb = x * np.exp(-2j * np.pi * tone * k / sr)
    alpha = 1.0 - np.exp(-2.0 * np.pi * (_COND_BW_HZ * 0.5) / sr)
    b, a = [alpha], [1.0, -(1.0 - alpha)]
    y = lfilter(b, a, lfilter(b, a, bb))
    out = np.real(y * np.exp(2j * np.pi * _OUT_PITCH * k / sr)).astype(np.float32)
    return (out / (np.abs(out).max() + 1e-9)).astype(np.float32)


def _keying_ratio(audio, sr):
    a = np.asarray(audio, dtype=np.float32)
    if a.size < 256:
        return 0.0
    hop = max(1, int(sr * _FRAME_MS / 1000))
    nper = min(max(hop * 2, 512), a.size)
    f, _t, Z = stft(a, fs=sr, nperseg=nper, noverlap=nper - hop, boundary=None)
    mag = np.abs(Z)
    band = (f >= _CW_LO) & (f <= _CW_HI)
    if not band.any() or mag.shape[1] < 4:
        return 0.0
    sub = mag[band]
    if sub.shape[0] >= 3:
        sub = np.stack([sub[max(0, i - 1):i + 2].sum(0) for i in range(sub.shape[0])])
    p10 = np.percentile(sub, 10, axis=1)
    p90 = np.percentile(sub, 90, axis=1)
    return float(np.max(p90 / (p10 + 1e-3)))


def _spectrogram(x):
    """numpy twin of torch.stft(center=True, reflect) -> |.| -> band -> log1p -> standardise."""
    x = np.pad(np.asarray(x, dtype=np.float32), _N_FFT // 2, mode='reflect')
    n_frames = 1 + (len(x) - _N_FFT) // _HOP
    idx = np.arange(_N_FFT)[None, :] + _HOP * np.arange(n_frames)[:, None]
    mag = np.abs(np.fft.rfft(x[idx] * _HANN, axis=1)).T[_LO_BIN:_HI_BIN]   # [65, T]
    spec = np.log1p(mag)
    spec = (spec - spec.mean()) / (spec.std(ddof=1) + 1e-6)
    return spec.astype(np.float32)


# What this module's vendored front end implements.  A model whose .json
# declares anything else was trained on different features: refuse it loudly
# rather than feed it spectrograms it would silently mis-decode.
_PREPROC = {'sample_rate': SR, 'n_fft': _N_FFT, 'hop_length': _HOP,
            'band_lo_hz': int(_BAND_LO_HZ), 'band_hi_hz': int(_BAND_HI_HZ),
            'freq_bins': _HI_BIN - _LO_BIN, 'window': 'hann', 'center': True,
            'magnitude': 'abs', 'compress': 'log1p',
            'normalize': 'global_standardize'}


def check_model_meta(meta):
    """Raise ValueError if the model's declared preprocessing/IO differs from
    what this module implements."""
    pre = meta.get('preprocessing', {})
    bad = {k: (pre.get(k), v) for k, v in _PREPROC.items() if pre.get(k) != v}
    if meta.get('input', {}).get('layout') != '[batch,1,freq=65,time]':
        bad['input.layout'] = (meta.get('input', {}).get('layout'), '[batch,1,freq=65,time]')
    if bad:
        raise ValueError('model preprocessing differs from deepfist_pass.py '
                         '(model, ours): %r -- port the new front end first' % bad)


class DeepFistOnnx:
    """second_pass decoder "deepfist". Config keys: model (path to the .onnx;
    its .json sidecar must sit next to it), threads (onnxruntime intra-op)."""
    name = 'deepfist'

    def __init__(self, cfg, threads=1):
        if isinstance(cfg, str):            # DeepFistOnnx(path) still works
            cfg = {'model': cfg, 'threads': threads}
        model_path = cfg.get('model', 'models/deepfist.onnx')
        threads = int(cfg.get('threads', 1))
        import onnxruntime as ort
        meta = json.load(open(model_path + '.json'))
        check_model_meta(meta)
        self._tokens = meta['tokens']
        self._blank = meta['ctc']['blank_index']
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(model_path, so, providers=['CPUExecutionProvider'])
        self._in = self._sess.get_inputs()[0].name

    def _ctc(self, log_probs):
        out, prev = [], None
        for s in log_probs[:, 0, :].argmax(-1):
            if s != prev and s != self._blank:
                out.append(self._tokens[s])
            prev = s
        return ''.join(out)

    @staticmethod
    def iq_to_audio(iq_interleaved):
        """1 kHz complex baseband (interleaved I,Q) -> SR real audio with the
        signal at 600 Hz, peak-normalised."""
        z = iq_interleaved[0::2].astype(np.float64) + 1j * iq_interleaved[1::2]
        z = resample_poly(z.real, 16, 5) + 1j * resample_poly(z.imag, 16, 5)   # 1k -> 3.2k
        t = np.arange(z.size) / SR
        a = np.real(z * np.exp(2j * np.pi * _OUT_PITCH * t))
        return (a / (np.abs(a).max() + 1e-12) * 0.9).astype(np.float32)

    def decode_audio(self, a, first_look=False):
        """Audio at SR -> text: 15 s windows, keying squelch, conditioner, net, CTC.
        first_look: decode only the single most-keyed window (the squelch score
        is ~8 ms per window, the net ~135 ms) -- a cheap look at a new bin."""
        win = int(WIN_SEC * SR)
        segs = [a[i:i + win] for i in range(0, len(a), win)]
        scored = [(_keying_ratio(seg, SR), seg) for seg in segs if len(seg) >= SR]
        scored = [(k, seg) for k, seg in scored if k >= _SQUELCH_THRESH]
        if first_look and scored:
            scored = [max(scored, key=lambda ks: ks[0])]
        parts = []
        for _k, seg in scored:
            spec = _spectrogram(_condition(seg))[None, None]
            text = self._ctc(self._sess.run(None, {self._in: spec})[0])
            if text.strip():
                parts.append(text)
        return ' '.join(parts)

    def decode_iq(self, iq_interleaved, first_look=False):
        return self.decode_audio(self.iq_to_audio(iq_interleaved), first_look)
