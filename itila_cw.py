#!/usr/bin/env python3
"""
ITILA-based Bayesian CW decoder prototype — M1/M2/M3/M4
Arc, 2026-04-14

Architecture (MacKay ITILA Ch. 16, 25, 28, 34):
  - 2-state {MARK, SPACE} HMM with geometric duration distributions
  - Speed w (WPM) as discrete latent variable: N_SPEED_BINS bins, marginalized via
    weighted sum of parallel forward-backward passes
  - EM (Baum-Welch) to estimate signal amplitude A and noise variance sigma2
  - Evidence ratio for signal presence (Ch. 28): log P(y|CW) - log P(y|noise)
  - Character decoding from soft posterior by threshold + run-length classification

Usage:
  python3 itila_cw.py --wav /path/to/B1.wav --freq 7040.0 --start 15 --end 30
  python3 itila_cw.py --eval --wav /path/to/B1.wav --key /path/to/key.txt
"""

import argparse
import wave
import struct
import sys
import ctypes
import os
import numpy as np
from collections import defaultdict

# ---------------------------------------------------------------------------
# Optional C-accelerated forward-backward core
# ---------------------------------------------------------------------------
_fb_lib = None
def _load_fb_lib():
    global _fb_lib
    if _fb_lib is not None:
        return _fb_lib
    so = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fb_core.so')
    if not os.path.exists(so):
        return None
    try:
        lib = ctypes.CDLL(so)
        lib.fb_core.restype = None
        lib.fb_core.argtypes = [
            ctypes.POINTER(ctypes.c_double),  # log_B
            ctypes.POINTER(ctypes.c_double),  # log_T
            ctypes.c_int,                     # T
            ctypes.POINTER(ctypes.c_double),  # log_alpha
            ctypes.POINTER(ctypes.c_double),  # log_beta
            ctypes.POINTER(ctypes.c_double),  # log_Z_out
        ]
        _fb_lib = lib
        return lib
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BAYES_RATE  = 200      # Hz — envelope sample rate after decimation
N_SPEED_BINS = 16
WPM_MIN, WPM_MAX = 8, 60
SPEED_BINS = np.linspace(WPM_MIN, WPM_MAX, N_SPEED_BINS)

# Morse character table
MORSE_TABLE = {
    '.-': 'A', '-...': 'B', '-.-.': 'C', '-..': 'D', '.': 'E',
    '..-.': 'F', '--.': 'G', '....': 'H', '..': 'I', '.---': 'J',
    '-.-': 'K', '.-..': 'L', '--': 'M', '-.': 'N', '---': 'O',
    '.--.': 'P', '--.-': 'Q', '.-.': 'R', '...': 'S', '-': 'T',
    '..-': 'U', '...-': 'V', '.--': 'W', '-..-': 'X', '-.--': 'Y',
    '--..': 'Z', '-----': '0', '.----': '1', '..---': '2',
    '...--': '3', '....-': '4', '.....': '5', '-....': '6',
    '--...': '7', '---..': '8', '----.': '9', '..--..': '?',
    '.-.-.-': '.', '--..--': ',', '-..-.': '/', '-....-': '-',
    '-.--.-': ')', '-.--.': '(', '.--.-.': '@',
}

# ---------------------------------------------------------------------------
# IQ file utilities
# ---------------------------------------------------------------------------

def _decode_iq_samples(raw, n_ch, sw, n_read, negate_q):
    """Decode raw WAV bytes to float64 I, Q arrays."""
    if sw == 2:
        samps = np.frombuffer(raw, dtype='<i2').astype(np.float64) / 32768.0
    elif sw == 3:
        raw_b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        vals = (raw_b[:, 0].astype(np.int32)
                | (raw_b[:, 1].astype(np.int32) << 8)
                | (raw_b[:, 2].astype(np.int32) << 16))
        vals[vals >= (1 << 23)] -= (1 << 24)
        samps = vals.astype(np.float64) / 8388608.0
    elif sw == 4:
        samps = np.frombuffer(raw, dtype='<i4').astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"Unsupported sample width {sw}")

    if n_ch == 2:
        I = samps[0::2]
        Q = -samps[1::2] if negate_q else samps[1::2]
    else:
        I = samps
        Q = np.zeros_like(I)
    return I, Q


def find_cw_tone_hz(I, Q, offset_hz, fs, tone_lo=200, tone_hi=3000, n_fft=8192):
    """Find the dominant CW carrier offset from the target frequency.

    In IQ recording, after mixing by offset_hz the target frequency is at DC.
    The actual CW carrier appears near DC (within ±500 Hz typically).
    We search in [20, 1500] Hz — avoids DC bias while finding the carrier fine
    offset even when the target is tuned 0.3 kHz away from the actual station.

    The tone_lo/tone_hi parameters are kept for API compatibility but ignored.
    Returns the residual carrier offset in Hz (to be added to offset_hz).
    """
    t = np.arange(min(len(I), n_fft), dtype=np.float64) / fs
    bb = (I[:len(t)] + 1j * Q[:len(t)]) * np.exp(-2j * np.pi * offset_hz * t)

    N = len(t)
    fft = np.fft.fft(bb)
    psd = np.abs(fft)**2
    freqs = np.fft.fftfreq(N, 1.0 / fs)

    # Search for carrier in [20, 1500] Hz — covers stations within ±1.5 kHz
    # of the target while excluding DC bias (< 20 Hz).
    mask = (freqs >= 20.0) & (freqs <= 1500.0)
    if not mask.any():
        return 0.0
    idx = np.argmax(psd * mask)
    return float(freqs[idx])


def load_iq_wav(path, start_sec=0, end_sec=None, negate_q=True):
    """Load raw I,Q arrays and sample rate from a WAV file window.

    Returns a cache tuple (I_head, Q_head, fs, bb_48k) where:
      I_head, Q_head : first 8192 samples at full rate (for tone finding)
      fs             : original sample rate (e.g. 192000)
      bb_48k         : complex IQ decimated 4:1 to ~48 kHz (for fast mixing)

    The 4:1 pre-decimation reduces the mixing cost from 23M→5.75M samples per
    channel, giving a ~4× speedup.  Aliasing from the sinc anti-alias filter is
    negligible after the subsequent 200 Hz channelizing LPF.
    """
    with wave.open(path, 'rb') as wf:
        n_ch  = wf.getnchannels()
        fs    = wf.getframerate()
        n_tot = wf.getnframes()
        sw    = wf.getsampwidth()

        start_frame = int(start_sec * fs)
        end_frame   = int(end_sec * fs) if end_sec else n_tot
        end_frame   = min(end_frame, n_tot)
        n_read      = end_frame - start_frame
        wf.setpos(start_frame)
        raw = wf.readframes(n_read)

    I, Q = _decode_iq_samples(raw, n_ch, sw, n_read, negate_q)
    del raw  # free raw bytes immediately

    # Pre-decimate to 48 kHz (4:1 block-average) for fast per-channel mixing.
    dec4 = 4
    n48  = len(I) // dec4
    bb_48k = (I[:n48*dec4].reshape(n48, dec4).mean(axis=1) +
              1j * Q[:n48*dec4].reshape(n48, dec4).mean(axis=1))

    # Keep only 8192 full-rate samples for find_cw_tone_hz.
    I_head = I[:8192].copy()
    Q_head = Q[:8192].copy()
    del I, Q  # free large full-rate arrays

    return I_head, Q_head, fs, bb_48k


def read_iq_wav(path, center_khz, target_khz, start_sec=0, end_sec=None,
                negate_q=True, tone_lo_hz=200, tone_hi_hz=3000,
                iq_cache=None):
    """Read B1-style IQ WAV (2ch, 192kHz) and extract CW envelope at target_khz.

    Pipeline:
      1. Decode IQ samples (16/24/32-bit)
      2. Coarse+fine frequency shift to bring CW tone to DC
      3. Decimate 192 kHz → 2 kHz (block-average, linear phase)
      4. Channelizing LPF at 200 Hz (Butterworth order 6, IIR applied at 2 kHz)
         — group delay ≈ 5ms at DC, negligible vs 54ms dits at 22 WPM
         — rejects adjacent stations at 300+ Hz spacing
      5. Envelope = |filtered complex signal|
      6. Decimate 2 kHz → BAYES_RATE=200 Hz (block-average)

    KEY FIX vs prior version: magnitude is phase-invariant (|z·e^{jθ}|=|z|), so
    frequency shift has no effect on wideband magnitude.  The channelizing LPF
    (step 4) is required to select one station before computing the envelope.
    The IIR group delay at 2 kHz is ~5ms — acceptable for CW at ≥15 WPM.

    Returns (envelope, BAYES_RATE).
    """
    from scipy.signal import butter, sosfilt

    if iq_cache is not None:
        I_head, Q_head, fs, bb_48k = iq_cache
    else:
        I_head, Q_head, fs, bb_48k = load_iq_wav(path, start_sec, end_sec, negate_q)

    # Coarse shift to target_khz baseband
    offset_hz = (target_khz - center_khz) * 1000.0

    # Find actual CW tone using the 8192-sample head (fast, no large allocation)
    tone_hz = find_cw_tone_hz(I_head, Q_head, offset_hz, fs, tone_lo_hz, tone_hi_hz)

    # Fine shift: bring CW tone to DC — operate on 48 kHz pre-decimated data
    # (5.75M samples vs 23M at full rate — 4× fewer exp evaluations)
    fs_48k = fs // 4  # 48000 Hz
    total_offset = offset_hz + tone_hz
    N_48 = len(bb_48k)
    t_48 = np.arange(N_48, dtype=np.float64) / fs_48k
    bb = bb_48k * np.exp(-2j * np.pi * total_offset * t_48)
    del t_48

    # Stage 1 decimate: 48 kHz → 2 kHz (24:1 block-average, sinc anti-alias)
    fs_mid = 2000
    dec1 = fs_48k // fs_mid  # 24
    n_mid = len(bb) // dec1
    bb_mid = bb[:n_mid * dec1].reshape(n_mid, dec1).mean(axis=1)
    del bb

    # Channelizing LPF at 200 Hz — select this station, reject adjacent ones.
    # Order 6 Butterworth at fc/fs_nyq=0.2 → group delay at DC ≈ 5ms.
    sos = butter(6, 200.0 / (fs_mid / 2.0), btype='low', output='sos')
    bb_i = sosfilt(sos, np.real(bb_mid).astype(np.float64))
    bb_q = sosfilt(sos, np.imag(bb_mid).astype(np.float64))

    # Envelope = |filtered complex signal|
    env_mid = np.sqrt(bb_i**2 + bb_q**2)

    # Stage 2 decimate: 2 kHz → BAYES_RATE=200 Hz (10:1 block-average)
    dec2 = fs_mid // BAYES_RATE  # 10
    n_out = len(env_mid) // dec2
    env = env_mid[:n_out * dec2].reshape(n_out, dec2).mean(axis=1)

    return env, BAYES_RATE


# ---------------------------------------------------------------------------
# HMM building blocks
# ---------------------------------------------------------------------------

def unit_samples(wpm):
    """Samples per Morse unit at given WPM (at BAYES_RATE)."""
    return max(1.0, (1200.0 / wpm) * BAYES_RATE / 1000.0)


def transition_probs(wpm):
    """Return (p_01, p_10): P(SPACE→MARK), P(MARK→SPACE) for geometric durations.

    Expected MARK duration: (1*P_dit + 3*P_dah) = ~2 units (assuming equal dit/dah)
    Expected SPACE duration: (1*P_es + 3*P_ls + 7*P_ws) = ~2.5 units average
    """
    d = unit_samples(wpm)
    p_mark_to_space = 1.0 / (2.0 * d)   # leave mark after avg 2 units
    p_space_to_mark = 1.0 / (2.5 * d)   # leave space after avg 2.5 units
    # Clamp to valid probability range
    p_mark_to_space = np.clip(p_mark_to_space, 1e-6, 0.5)
    p_space_to_mark = np.clip(p_space_to_mark, 1e-6, 0.5)
    return p_space_to_mark, p_mark_to_space


def forward_backward(env, wpm, A, sigma2):
    """Run sum-product forward-backward on 2-state {SPACE=0, MARK=1} HMM.

    Observation model:
      P(y_t | MARK) = N(A, sigma2)
      P(y_t | SPACE) = N(0, sigma2)  [noise-only Gaussian]

    Returns:
      gamma: (T, 2) array of marginal posteriors P(s_t | y)
      log_Z: log marginal likelihood log P(y | w, A, sigma2)
    """
    T = len(env)
    p01, p10 = transition_probs(wpm)

    # Log transition matrix: log_T[s, s'] = log P(s_t+1=s' | s_t=s)
    log_T = np.log(np.array([
        [1 - p01, p01],
        [p10,     1 - p10]
    ]) + 1e-300)

    # Log observation likelihoods: (T, 2)
    log_norm = -0.5 * np.log(2 * np.pi * sigma2)
    log_B = np.empty((T, 2))
    log_B[:, 0] = log_norm - 0.5 * env**2 / sigma2            # SPACE
    log_B[:, 1] = log_norm - 0.5 * (env - A)**2 / sigma2      # MARK

    # Forward pass (log-space)
    log_alpha = np.empty((T, 2))
    log_alpha[0] = np.log(0.5) + log_B[0]
    for t in range(1, T):
        for s in range(2):
            log_alpha[t, s] = log_B[t, s] + np.logaddexp(
                log_alpha[t - 1, 0] + log_T[0, s],
                log_alpha[t - 1, 1] + log_T[1, s],
            )

    # Backward pass (log-space)
    log_beta = np.zeros((T, 2))  # log 1 = 0 at T-1
    for t in range(T - 2, -1, -1):
        for s in range(2):
            log_beta[t, s] = np.logaddexp(
                log_T[s, 0] + log_B[t + 1, 0] + log_beta[t + 1, 0],
                log_T[s, 1] + log_B[t + 1, 1] + log_beta[t + 1, 1],
            )

    # Marginals
    log_gamma = log_alpha + log_beta
    log_Z_t = np.logaddexp(log_gamma[:, 0], log_gamma[:, 1])
    gamma = np.exp(log_gamma - log_Z_t[:, np.newaxis])

    # Log marginal likelihood: log P(y)
    log_Z = np.logaddexp(log_alpha[-1, 0], log_alpha[-1, 1])

    return gamma, log_Z


def forward_backward_fast(env, wpm, A, noise_mean, sigma2_obs):
    """2-state HMM forward-backward.  Uses fb_core.so (C) when available.

    Observation model (Gaussian mixture — parameters estimated directly from data):
      SPACE: N(noise_mean, sigma2_obs)
      MARK:  N(A,          sigma2_obs)
    """
    T = len(env)
    p01, p10 = transition_probs(wpm)
    T_mat = np.array([[1 - p01, p01], [p10, 1 - p10]])
    log_T_flat = np.log(T_mat.ravel() + 1e-300)  # row-major [T00,T01,T10,T11]

    log_norm = -0.5 * np.log(2 * np.pi * sigma2_obs)
    log_B = np.empty((T, 2), dtype=np.float64)
    log_B[:, 0] = log_norm - 0.5 * (env - noise_mean)**2 / sigma2_obs  # SPACE
    log_B[:, 1] = log_norm - 0.5 * (env - A)**2 / sigma2_obs           # MARK

    lib = _load_fb_lib()
    if lib is not None:
        log_B_c   = np.ascontiguousarray(log_B,   dtype=np.float64)
        log_alpha = np.empty((T, 2), dtype=np.float64)
        log_beta  = np.empty((T, 2), dtype=np.float64)
        log_Z_buf = np.empty(1,      dtype=np.float64)
        lib.fb_core(
            log_B_c.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            log_T_flat.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.c_int(T),
            log_alpha.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            log_beta.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            log_Z_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        )
        log_gamma = log_alpha + log_beta
        log_Z_t = np.logaddexp(log_gamma[:, 0], log_gamma[:, 1])
        gamma = np.exp(log_gamma - log_Z_t[:, np.newaxis])
        return gamma, float(log_Z_buf[0])

    # Pure-Python fallback (slow)
    log_T = log_T_flat.reshape(2, 2)
    log_alpha = np.empty((T, 2))
    log_alpha[0] = np.log(0.5) + log_B[0]
    for t in range(1, T):
        a0, a1 = log_alpha[t-1]
        log_alpha[t, 0] = log_B[t, 0] + np.logaddexp(log_T[0,0]+a0, log_T[1,0]+a1)
        log_alpha[t, 1] = log_B[t, 1] + np.logaddexp(log_T[0,1]+a0, log_T[1,1]+a1)
    log_beta = np.zeros((T, 2))
    for t in range(T - 2, -1, -1):
        lb0 = log_B[t+1, 0] + log_beta[t+1, 0]
        lb1 = log_B[t+1, 1] + log_beta[t+1, 1]
        log_beta[t, 0] = np.logaddexp(log_T[0,0]+lb0, log_T[0,1]+lb1)
        log_beta[t, 1] = np.logaddexp(log_T[1,0]+lb0, log_T[1,1]+lb1)
    log_gamma = log_alpha + log_beta
    log_Z_t = np.logaddexp(log_gamma[:, 0], log_gamma[:, 1])
    gamma = np.exp(log_gamma - log_Z_t[:, np.newaxis])
    log_Z = np.logaddexp(log_alpha[-1, 0], log_alpha[-1, 1])
    return gamma, log_Z


# ---------------------------------------------------------------------------
# Speed marginalization
# ---------------------------------------------------------------------------

def decode_marginal(env, A, noise_mean, sigma2_obs, speed_bins=SPEED_BINS):
    """Run forward-backward over all speed bins, return marginalized posteriors.

    Returns:
      gamma_marg: (T, 2) marginalized P(s_t | y)
      best_wpm:   speed bin with highest evidence
      log_ev:     log P(y | w) for each speed bin (shape N_SPEED_BINS)
      speed_post: P(w | y) normalized posterior over speed bins
    """
    log_evs = np.zeros(len(speed_bins))
    gammas  = []
    for i, wpm in enumerate(speed_bins):
        g, lz = forward_backward_fast(env, wpm, A, noise_mean, sigma2_obs)
        log_evs[i] = lz
        gammas.append(g)

    # Uniform speed prior → posterior ∝ evidence
    log_ev_max = log_evs.max()
    log_sp = log_evs - (log_ev_max + np.log(np.sum(np.exp(log_evs - log_ev_max))))
    speed_post = np.exp(log_sp)

    # Weighted marginal posterior
    gamma_marg = sum(w * g for w, g in zip(speed_post, gammas))
    best_wpm = speed_bins[np.argmax(log_evs)]

    return gamma_marg, best_wpm, log_evs, speed_post


# ---------------------------------------------------------------------------
# EM (Baum-Welch) for A and sigma2 estimation
# ---------------------------------------------------------------------------

def estimate_wpm_from_gamma(gamma, min_runs=5):
    """Estimate WPM from mark run lengths in HMM posterior.

    Uses gamma[:,1] > 0.5 to get a binary mark/space sequence, then fits
    the dit duration from the 25th percentile of mark run lengths.

    Returns estimated WPM, or None if insufficient marks detected.
    """
    marks = (gamma[:, 1] > 0.5).astype(int)

    runs = []
    val, cnt = marks[0], 1
    for i in range(1, len(marks)):
        if marks[i] == val:
            cnt += 1
        else:
            runs.append((val, cnt))
            val, cnt = marks[i], 1
    runs.append((val, cnt))

    mark_runs = [c for v, c in runs if v == 1 and c >= 2]
    if len(mark_runs) < min_runs:
        return None

    # P25 = dit duration estimate (shorter runs are dits, longer are dahs)
    dit_samples = float(np.percentile(mark_runs, 25))
    if dit_samples < 1:
        return None
    wpm = 1200.0 / (dit_samples / BAYES_RATE * 1000.0)
    return float(np.clip(wpm, WPM_MIN, WPM_MAX))


def em_estimate(env, wpm_init=25.0, n_iter=10, A_init=None, noise_mean_init=None, sigma2_obs_init=None):
    """Two-phase EM to estimate A, noise_mean, sigma2_obs, and WPM jointly.

    Gaussian mixture model:
      SPACE ~ N(noise_mean, sigma2_obs)
      MARK  ~ N(A,          sigma2_obs)

    Parameters estimated from the data directly — no Rayleigh assumption.

    Phase 1 (n_iter//2 iterations): run at wpm_init to get rough estimates.
    Phase 2 (n_iter//2 iterations): re-estimate WPM from gamma, then refine.

    Returns (A, noise_mean, sigma2_obs, gamma, wpm_est).
    """
    # Initialize from data percentiles
    if noise_mean_init is None:
        noise_mean_init = float(np.percentile(env, 30))
    if sigma2_obs_init is None:
        # Variance of lower-envelope (noise) samples
        lo = env[env < np.percentile(env, 40)]
        sigma2_obs_init = max(float(np.var(lo)) if len(lo) > 2 else (noise_mean_init * 0.2)**2, 1e-20)
    if A_init is None:
        # Use samples well above noise level as signal estimate
        thresh = noise_mean_init + 3 * np.sqrt(sigma2_obs_init)
        hi = env[env > thresh]
        A_init = float(np.mean(hi)) if len(hi) > 10 else float(np.percentile(env, 95))

    A, noise_mean, sigma2_obs, wpm = A_init, noise_mean_init, sigma2_obs_init, wpm_init
    half = max(n_iter // 2, 3)

    def _em_step(wpm, A, noise_mean, sigma2_obs, n):
        for _ in range(n):
            g, _ = forward_backward_fast(env, wpm, A, noise_mean, sigma2_obs)
            gm = g[:, 1]
            gs = g[:, 0]
            dm = gm.sum() + 1e-10
            ds = gs.sum() + 1e-10
            # M-step: update means
            A_new = max(float((gm * env).sum() / dm), noise_mean + 1e-10)
            noise_mean_new = float((gs * env).sum() / ds)
            # Pooled observed variance (same sigma for both states)
            vm = float((gm * (env - A_new)**2).sum() / dm)
            vs = float((gs * (env - noise_mean_new)**2).sum() / ds)
            sigma2_obs = max((vm + vs) / 2.0, 1e-20)
            A, noise_mean = A_new, noise_mean_new
        g, _ = forward_backward_fast(env, wpm, A, noise_mean, sigma2_obs)
        return A, noise_mean, sigma2_obs, g

    # Phase 1: rough estimates at wpm_init
    A, noise_mean, sigma2_obs, gamma = _em_step(wpm, A, noise_mean, sigma2_obs, half)

    # Phase 2: re-estimate WPM from gamma, refine
    wpm_new = estimate_wpm_from_gamma(gamma)
    if wpm_new is not None:
        wpm = wpm_new
    A, noise_mean, sigma2_obs, gamma = _em_step(wpm, A, noise_mean, sigma2_obs, n_iter - half)

    return A, noise_mean, sigma2_obs, gamma, wpm


# ---------------------------------------------------------------------------
# M2: Signal detection via evidence ratio (MacKay Ch. 28)
# ---------------------------------------------------------------------------

def signal_evidence_ratio(env, A, noise_mean, sigma2_obs):
    """Compute log Bayes factor: log P(y | CW model) - log P(y | noise model).

    Noise model: all samples ~ N(noise_mean, sigma2_obs) independently.
    CW model: best log_Z from forward-backward (marginalized over speed).

    A positive ratio means the data is more likely under the CW model.
    """
    # Noise-only log likelihood: P(y | H=0) = prod_t N(y_t; noise_mean, sigma2_obs)
    log_lik_noise = np.sum(-0.5 * (env - noise_mean)**2 / sigma2_obs
                           - 0.5 * np.log(2 * np.pi * sigma2_obs))

    # CW model log likelihood: marginalized over speed
    _, _, log_evs, _ = decode_marginal(env, A, noise_mean, sigma2_obs)
    log_ev_max = log_evs.max()
    log_lik_cw = log_ev_max + np.log(np.mean(np.exp(log_evs - log_ev_max)))

    log_bayes_factor = log_lik_cw - log_lik_noise
    return log_bayes_factor


# ---------------------------------------------------------------------------
# Character decoding from mark/space posterior
# ---------------------------------------------------------------------------

def posterior_to_marks(gamma, threshold=0.5):
    """Threshold gamma[:,1] to produce binary mark/space sequence."""
    return (gamma[:, 1] > threshold).astype(np.int8)


def decode_runs(marks, wpm):
    """Decode binary mark/space sequence to Morse characters.

    Uses run-length classification against expected unit duration at wpm.
    Returns decoded string.
    """
    if marks is None or len(marks) == 0:
        return ''

    unit = unit_samples(wpm)

    # Find runs of 0s and 1s
    runs = []
    val = marks[0]
    count = 1
    for i in range(1, len(marks)):
        if marks[i] == val:
            count += 1
        else:
            runs.append((val, count))
            val = marks[i]
            count = 1
    runs.append((val, count))

    # Classify runs
    # dit: mark duration < 2 units
    # dah: mark duration >= 2 units
    # element-space: space < 2 units → separator within symbol
    # letter-space:  2 <= space < 5 units → between letters
    # word-space:    space >= 5 units → between words

    current_symbol = ''
    text = ''

    for (is_mark, dur) in runs:
        if is_mark:
            if dur < 2 * unit:
                current_symbol += '.'
            else:
                current_symbol += '-'
        else:
            if dur < 2 * unit:
                pass  # element separator, already handled by next mark
            elif dur < 5 * unit:
                # letter-space: emit current symbol
                if current_symbol:
                    text += MORSE_TABLE.get(current_symbol, '?')
                    current_symbol = ''
            else:
                # word-space
                if current_symbol:
                    text += MORSE_TABLE.get(current_symbol, '?')
                    current_symbol = ''
                text += ' '

    if current_symbol:
        text += MORSE_TABLE.get(current_symbol, '?')

    return text


# ---------------------------------------------------------------------------
# Channel scanner — find signals in a frequency band
# ---------------------------------------------------------------------------

def scan_channel(env, wpm, A, sigma2, snr_threshold=8.0):
    """Decide if a channel contains a CW signal worth decoding.

    Returns True if signal-to-noise ratio in marks vs spaces looks like CW.
    Fast pre-screen — not the full evidence ratio.
    """
    # Quick estimate: RMS in estimated mark regions vs space regions
    rough_mark = env > A * 0.3  # rough threshold
    rms_on  = np.sqrt(np.mean(env[rough_mark]**2))  if rough_mark.any()  else 0
    rms_off = np.sqrt(np.mean(env[~rough_mark]**2)) if (~rough_mark).any() else 1
    snr_db = 20 * np.log10((rms_on + 1e-10) / (rms_off + 1e-10))
    return snr_db >= snr_threshold, snr_db


# ---------------------------------------------------------------------------
# Callsign extraction from decoded text
# ---------------------------------------------------------------------------

def extract_callsigns(text, valid_calls=None):
    """Find plausible callsigns in decoded text.

    Uses simple heuristic: tokens matching callsign pattern (letter/digit mix,
    3–7 chars, starts with letter or digit).
    """
    import re
    tokens = re.findall(r'[A-Z0-9]{3,7}', text.upper())
    callsigns = []
    for tok in tokens:
        if re.match(r'^[A-Z0-9]{1,2}[0-9][A-Z0-9]{1,4}$', tok):
            if valid_calls is None or tok in valid_calls:
                callsigns.append(tok)
    return callsigns


# ---------------------------------------------------------------------------
# Full single-channel decode pipeline
# ---------------------------------------------------------------------------

def decode_channel(env, center_khz, freq_khz, start_sec=0, em_iter=8,
                   evidence_threshold=10.0, verbose=False):
    """Full pipeline for a single channel: EM → speed marginal → decode.

    Returns dict with keys: wpm, A, sigma2, text, callsigns, log_bayes, gamma
    """
    if len(env) < BAYES_RATE:  # less than 1 second
        return None

    # EM to estimate A, noise_mean, sigma2_obs, and initial WPM
    A, noise_mean, sigma2_obs, gamma_init, wpm_est = em_estimate(
        env, wpm_init=25.0, n_iter=em_iter)

    if verbose:
        sigma_obs = np.sqrt(sigma2_obs)
        print(f"  {freq_khz:.2f} kHz: A={A:.4f} noise_mean={noise_mean:.4f} sigma_obs={sigma_obs:.4f}", flush=True)

    # M2: Evidence ratio — skip channels that look like noise
    log_bf = signal_evidence_ratio(env, A, noise_mean, sigma2_obs)
    if verbose:
        print(f"  log Bayes factor: {log_bf:.1f}", flush=True)

    if log_bf < evidence_threshold:
        return {'log_bayes': log_bf, 'wpm': 0, 'text': '', 'callsigns': []}

    # Speed marginalization — full range for posterior, but use EM WPM
    # The raw evidence curve is biased toward low WPM (persistence bonus from
    # smaller transition probs).  Use EM WPM for decoding, which jointly
    # optimizes A/noise_mean/sigma2_obs/WPM without the persistence bias.
    gamma_marg, _, log_evs, speed_post = decode_marginal(
        env, A, noise_mean, sigma2_obs)
    best_wpm = wpm_est  # EM WPM is more reliable than evidence argmax

    # Decode to text using EM-estimated WPM
    marks = posterior_to_marks(gamma_marg)
    text  = decode_runs(marks, best_wpm)

    callsigns = extract_callsigns(text)

    if verbose:
        ev_best_wpm = SPEED_BINS[np.argmax(log_evs)]
        print(f"  wpm={best_wpm:.0f} (em), ev_wpm={ev_best_wpm:.0f}  text={repr(text[:80])}", flush=True)
        if callsigns:
            print(f"  CALLSIGNS: {callsigns}", flush=True)

    return {
        'freq_khz':   freq_khz,
        'wpm':        best_wpm,
        'A':          A,
        'noise_mean': noise_mean,
        'sigma2_obs': sigma2_obs,
        'log_bayes':  log_bf,
        'text':       text,
        'callsigns':  callsigns,
        'gamma':      gamma_marg,
    }


# ---------------------------------------------------------------------------
# Eval mode — scan B1 recording against gold key
# ---------------------------------------------------------------------------

def run_eval(wav_path, center_khz, key_path, start_min=15, end_min=30,
             freq_step_khz=0.1, band_khz_min=None, band_khz_max=None,
             evidence_threshold=10.0, verbose=False):
    """Scan a frequency range in the B1 recording and score against gold key."""

    # Load gold key
    with open(key_path) as f:
        gold = set(f.read().strip().upper().replace('\n', ',').split(','))
    gold = {c.strip() for c in gold if c.strip()}
    print(f"Gold key: {len(gold)} calls", flush=True)

    start_sec = start_min * 60.0
    end_sec   = end_min   * 60.0

    # Default band
    if band_khz_min is None:
        band_khz_min = center_khz - 90
    if band_khz_max is None:
        band_khz_max = center_khz + 90

    freqs = np.arange(band_khz_min, band_khz_max, freq_step_khz)
    print(f"Scanning {len(freqs)} channels from {band_khz_min} to {band_khz_max} kHz", flush=True)

    found = set()
    total_channels = 0
    # Read WAV once per time chunk; loop over all frequencies using cached IQ.
    # A 2-minute chunk at 192 kHz stereo 16-bit = ~92 MB — fits in RAM easily.
    chunk_sec = 120.0

    t = start_sec
    chunk_num = 0
    while t < end_sec:
        t1 = min(t + chunk_sec, end_sec)
        chunk_num += 1
        print(f"Chunk {chunk_num}: {t/60:.0f}-{t1/60:.0f} min — loading IQ...",
              flush=True)
        try:
            iq_cache = load_iq_wav(wav_path, start_sec=t, end_sec=t1)
        except Exception as e:
            print(f"  WAV load error: {e}", flush=True)
            t = t1
            continue

        for i, freq in enumerate(freqs):
            try:
                env, _ = read_iq_wav(wav_path, center_khz, freq,
                                      iq_cache=iq_cache)
            except Exception as e:
                if verbose:
                    print(f"  {freq:.2f}: env error: {e}", flush=True)
                continue

            result = decode_channel(env, center_khz, freq,
                                     evidence_threshold=evidence_threshold,
                                     verbose=verbose)
            if result and result['callsigns']:
                for call in result['callsigns']:
                    if call in gold and call not in found:
                        found.add(call)
                        total_channels += 1
                        print(f"  HIT: {call} on {freq:.2f} kHz "
                              f"[chunk {chunk_num}]", flush=True)

        t = t1

    missed = gold - found
    score = len(found)
    total = len(gold)
    print(f"\nScore: {score}/{total}", flush=True)
    print(f"Found: {sorted(found)}", flush=True)
    print(f"Missed: {sorted(missed)}", flush=True)
    return score, total, found, missed


# ---------------------------------------------------------------------------
# Diagnostic mode — why did we miss what we missed?
# ---------------------------------------------------------------------------

def run_diag(wav_path, center_khz, key_path, start_min=15, end_min=30,
             freq_step_khz=0.3, band_khz_min=None, band_khz_max=None,
             diag_threshold=200.0, out_path=None):
    """Diagnostic scan: run at low threshold, track per-callsign best result.

    Categorizes each gold-key callsign:
      FOUND_AT_DIAG   — decoded when threshold=diag_threshold (threshold was too high)
      DECODED_GARBLED — callsign appeared in raw text but not extracted as callsign
      NOT_DECODED     — never appeared in any channel's text (signal too weak / decode fail)

    Output goes to out_path (default /tmp/itila_diag.txt).
    """
    out_path = out_path or '/tmp/itila_diag.txt'

    with open(key_path) as f:
        gold = set(f.read().strip().upper().replace('\n', ',').split(','))
    gold = {c.strip() for c in gold if c.strip()}

    start_sec = start_min * 60.0
    end_sec   = end_min   * 60.0

    if band_khz_min is None:
        band_khz_min = center_khz - 90
    if band_khz_max is None:
        band_khz_max = center_khz + 90

    freqs = np.arange(band_khz_min, band_khz_max, freq_step_khz)

    # Per-callsign tracking: best sighting across all chunks/freqs
    # call -> {'log_bf', 'freq', 'chunk', 'text', 'wpm', 'as_callsign': bool}
    call_best = {}

    chunk_sec = 120.0
    t = start_sec
    chunk_num = 0

    print(f"[diag] scanning {len(freqs)} channels, threshold={diag_threshold}, out={out_path}", flush=True)

    with open(out_path, 'w') as out:
        out.write(f"ITILA DIAGNOSTIC — {wav_path}\n")
        out.write(f"Gold key: {len(gold)} calls | threshold={diag_threshold}\n")
        out.write(f"Scan: {band_khz_min}-{band_khz_max} kHz step={freq_step_khz} kHz"
                  f"  {start_min}-{end_min} min\n")
        out.write("=" * 70 + "\n\n")

        while t < end_sec:
            t1 = min(t + chunk_sec, end_sec)
            chunk_num += 1
            out.write(f"\n=== Chunk {chunk_num}: {t/60:.0f}-{t1/60:.0f} min ===\n")
            out.flush()
            print(f"[diag] chunk {chunk_num}: {t/60:.0f}-{t1/60:.0f} min", flush=True)

            try:
                iq_cache = load_iq_wav(wav_path, start_sec=t, end_sec=t1)
            except Exception as e:
                out.write(f"  WAV load error: {e}\n")
                t = t1
                continue

            for freq in freqs:
                try:
                    env, _ = read_iq_wav(wav_path, center_khz, freq,
                                          iq_cache=iq_cache)
                except Exception:
                    continue

                result = decode_channel(env, center_khz, freq,
                                         evidence_threshold=diag_threshold,
                                         verbose=False)
                if result is None:
                    continue

                log_bf   = result.get('log_bayes', -1e9)
                text     = result.get('text', '')
                calls    = result.get('callsigns', [])
                wpm      = result.get('wpm', 0)
                text_up  = text.upper()

                # Check which gold calls appear in raw text (even if not extracted)
                gold_in_text = [c for c in gold if c in text_up]
                gold_calls   = [c for c in calls if c in gold]

                # Update per-callsign tracker
                for call in set(gold_in_text + gold_calls):
                    as_call = call in gold_calls
                    prev = call_best.get(call)
                    if prev is None or log_bf > prev['log_bf']:
                        call_best[call] = {
                            'log_bf':     log_bf,
                            'freq':       freq,
                            'chunk':      chunk_num,
                            'text':       text[:120],
                            'wpm':        wpm,
                            'as_callsign': as_call,
                        }

                # Log any channel that decoded something interesting
                if gold_in_text or gold_calls or (text and log_bf > diag_threshold):
                    flag = ''
                    if gold_calls:
                        flag = f"  *** CALLSIGN: {gold_calls}"
                    elif gold_in_text:
                        flag = f"  *** IN_TEXT: {gold_in_text}"
                    out.write(f"  {freq:8.2f} kHz  logBF={log_bf:8.1f}  wpm={wpm:4.0f}{flag}\n")
                    if gold_in_text or gold_calls:
                        out.write(f"    text: {repr(text[:100])}\n")

            t = t1

        # ---------------------------------------------------------------
        # Per-callsign summary
        # ---------------------------------------------------------------
        out.write("\n" + "=" * 70 + "\n")
        out.write("PER-CALLSIGN DIAGNOSTIC SUMMARY\n")
        out.write("=" * 70 + "\n\n")

        found_at_diag  = []
        garbled        = []
        not_decoded    = []

        for call in sorted(gold):
            b = call_best.get(call)
            if b is None:
                not_decoded.append(call)
            elif b['as_callsign']:
                found_at_diag.append(call)
                out.write(f"  FOUND_AT_DIAG  {call:10s}: logBF={b['log_bf']:8.1f}  "
                          f"freq={b['freq']:.2f}  chunk={b['chunk']}  wpm={b['wpm']:.0f}\n")
                out.write(f"    text: {repr(b['text'][:100])}\n")
            else:
                garbled.append(call)
                out.write(f"  GARBLED        {call:10s}: logBF={b['log_bf']:8.1f}  "
                          f"freq={b['freq']:.2f}  chunk={b['chunk']}  wpm={b['wpm']:.0f}\n")
                out.write(f"    text: {repr(b['text'][:100])}\n")

        out.write(f"\n--- NOT DECODED (never appeared in any channel text) ---\n")
        for call in not_decoded:
            out.write(f"  {call}\n")

        out.write(f"\nSUMMARY:\n")
        out.write(f"  Found at diag threshold ({diag_threshold}): {len(found_at_diag)}\n")
        out.write(f"  Decoded but garbled/not extracted:         {len(garbled)}\n")
        out.write(f"  Never decoded:                             {len(not_decoded)}\n")
        out.write(f"  Total gold: {len(gold)}\n")

    print(f"[diag] done. Results: {out_path}", flush=True)
    print(f"  Found at diag threshold: {len(found_at_diag)} — {found_at_diag}", flush=True)
    print(f"  Garbled/not extracted:   {len(garbled)} — {garbled}", flush=True)
    print(f"  Never decoded:           {len(not_decoded)} — {not_decoded}", flush=True)

    return found_at_diag, garbled, not_decoded


# ---------------------------------------------------------------------------
# Single-frequency decode mode
# ---------------------------------------------------------------------------

def run_single(wav_path, center_khz, target_khz, start_min=0, end_min=None,
               evidence_threshold=5.0, verbose=True):
    """Decode a single frequency and print results."""
    start_sec = start_min * 60.0
    end_sec   = end_min * 60.0 if end_min else None

    print(f"Decoding {target_khz:.3f} kHz from {wav_path}", flush=True)
    env, _ = read_iq_wav(wav_path, center_khz, target_khz,
                          start_sec=start_sec, end_sec=end_sec)
    print(f"  Envelope: {len(env)} samples ({len(env)/BAYES_RATE:.1f}s)", flush=True)

    result = decode_channel(env, center_khz, target_khz,
                             evidence_threshold=evidence_threshold,
                             verbose=verbose)
    if result:
        print(f"\nResult: {result['wpm']:.0f} WPM  logBF={result['log_bayes']:.1f}")
        print(f"Text: {result['text'][:200]}")
        print(f"Callsigns: {result['callsigns']}")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description='ITILA CW decoder prototype')
    ap.add_argument('--wav',      required=True, help='IQ WAV file path')
    ap.add_argument('--center',   type=float, default=7090.0,
                    help='Center frequency of recording (kHz)')
    ap.add_argument('--freq',     type=float, help='Target frequency (kHz) for single decode')
    ap.add_argument('--start',    type=float, default=0,  help='Start time (minutes)')
    ap.add_argument('--end',      type=float, default=None, help='End time (minutes)')
    ap.add_argument('--eval',     action='store_true', help='Run full eval mode')
    ap.add_argument('--key',      help='Gold key file path (for eval)')
    ap.add_argument('--band-min', type=float, help='Band min kHz (eval)')
    ap.add_argument('--band-max', type=float, help='Band max kHz (eval)')
    ap.add_argument('--step',     type=float, default=0.1, help='Frequency step kHz (eval)')
    ap.add_argument('--thresh',   type=float, default=10.0,
                    help='log Bayes factor threshold for signal detection')
    ap.add_argument('--diag',    action='store_true',
                    help='Diagnostic scan: run at low threshold, show per-callsign breakdown')
    ap.add_argument('--diag-thresh', type=float, default=200.0,
                    help='log BF threshold for diagnostic scan (default 200, vs 2000 for eval)')
    ap.add_argument('--diag-out',    default='/tmp/itila_diag.txt',
                    help='Output file for diagnostic results (default /tmp/itila_diag.txt)')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()

    if args.diag:
        if not args.key:
            print("--key required for diag mode", file=sys.stderr)
            sys.exit(1)
        run_diag(args.wav, args.center, args.key,
                 start_min=args.start,
                 end_min=args.end if args.end else 30,
                 freq_step_khz=args.step,
                 band_khz_min=args.band_min,
                 band_khz_max=args.band_max,
                 diag_threshold=args.diag_thresh,
                 out_path=args.diag_out)
    elif args.eval:
        if not args.key:
            print("--key required for eval mode", file=sys.stderr)
            sys.exit(1)
        run_eval(args.wav, args.center, args.key,
                 start_min=args.start,
                 end_min=args.end if args.end else 30,
                 freq_step_khz=args.step,
                 band_khz_min=args.band_min,
                 band_khz_max=args.band_max,
                 evidence_threshold=args.thresh,
                 verbose=args.verbose)
    elif args.freq:
        run_single(args.wav, args.center, args.freq,
                   start_min=args.start,
                   end_min=args.end,
                   evidence_threshold=args.thresh,
                   verbose=args.verbose)
    else:
        ap.print_help()


if __name__ == '__main__':
    main()
