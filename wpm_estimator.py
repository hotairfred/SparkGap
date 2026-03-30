#!/usr/bin/env python3
"""
wpm_estimator.py — Estimate CW speed from keying envelope autocorrelation.

Fallback WPM estimation for signals uhsdr can't decode (weak/slow).
Measures dit period from envelope autocorrelation peaks.

Usage:
    from wpm_estimator import estimate_wpm
    wpm, confidence = estimate_wpm(audio_samples, sample_rate=12000)
"""

import numpy as np


def estimate_wpm(audio, sample_rate=12000, tone_freq=600, window_sec=5.0):
    """Estimate CW speed from keying envelope autocorrelation.

    Args:
        audio: float32 array of channelized audio samples
        sample_rate: sample rate in Hz (typically 4000 or 12000)
        tone_freq: expected CW tone frequency in Hz
        window_sec: analysis window in seconds

    Returns:
        (wpm, confidence) tuple.
        wpm: estimated words per minute (5-60 range), 0 if no signal
        confidence: 0.0-1.0, how sharp the autocorrelation peak is
    """
    n = min(len(audio), int(window_sec * sample_rate))
    if n < sample_rate * 0.5:
        return 0, 0.0

    audio = audio[:n].astype(np.float32)

    # Step 1: Extract keying envelope via Goertzel-like bandpass + magnitude
    # Bandpass around tone_freq ± 100 Hz using complex mixing + lowpass
    t = np.arange(n) / sample_rate
    analytic = audio * np.exp(-2j * np.pi * tone_freq * t)

    # Lowpass at ~100 Hz (keeps CW keying, rejects off-frequency signals)
    # Simple exponential moving average, tau ~ 5ms
    lp_alpha = 2.0 * np.pi * 100.0 / sample_rate
    lp_alpha = min(lp_alpha, 1.0)
    envelope = np.zeros(n)
    acc = 0.0 + 0.0j
    for i in range(n):
        acc += lp_alpha * (analytic[i] - acc)
        envelope[i] = abs(acc)

    # Step 2: Smooth envelope (50 Hz lowpass for clean on/off transitions)
    smooth_alpha = 2.0 * np.pi * 50.0 / sample_rate
    smooth_alpha = min(smooth_alpha, 1.0)
    smoothed = np.zeros(n)
    acc = 0.0
    for i in range(n):
        acc += smooth_alpha * (envelope[i] - acc)
        smoothed[i] = acc

    # Normalize
    peak = np.max(smoothed)
    if peak < 1e-10:
        return 0, 0.0
    smoothed = smoothed / peak

    # Step 3: Threshold to binary keying (Otsu-like: use median as threshold)
    threshold = np.median(smoothed) + 0.15 * (np.max(smoothed) - np.median(smoothed))
    binary = (smoothed > threshold).astype(np.float32)

    # Remove DC offset for autocorrelation
    binary = binary - np.mean(binary)

    if np.std(binary) < 0.05:
        return 0, 0.0  # No keying detected

    # Step 4: Autocorrelation via FFT (fast)
    n_padded = 2 ** int(np.ceil(np.log2(2 * n)))
    fft_binary = np.fft.rfft(binary, n_padded)
    autocorr = np.fft.irfft(fft_binary * np.conj(fft_binary))[:n]
    autocorr = autocorr / autocorr[0]  # Normalize to 1.0 at lag 0

    # Step 5: Find first significant peak after lag 0
    # Dit period range: 5 WPM (240ms) to 60 WPM (20ms)
    min_lag = int(0.020 * sample_rate)  # 20ms = 60 WPM
    max_lag = int(0.300 * sample_rate)  # 300ms = 4 WPM

    if max_lag >= len(autocorr):
        max_lag = len(autocorr) - 1
    if min_lag >= max_lag:
        return 0, 0.0

    search = autocorr[min_lag:max_lag]

    # Find the highest peak in the search range
    peak_idx = np.argmax(search)
    peak_val = search[peak_idx]
    lag_samples = peak_idx + min_lag

    if peak_val < 0.1:
        return 0, 0.0  # No significant periodicity

    # Convert lag to WPM: dit_period_ms = lag_samples / sample_rate * 1000
    # WPM = 1200 / dit_period_ms
    dit_ms = lag_samples * 1000.0 / sample_rate
    if dit_ms < 15:
        return 0, 0.0

    wpm = 1200.0 / dit_ms

    # Clamp to reasonable range
    wpm = max(5.0, min(60.0, wpm))

    # Confidence: how sharp is the peak relative to the surrounding valley?
    valley = np.min(search[max(0, peak_idx - 5):peak_idx + 5 + 1]) if peak_idx > 5 else 0
    confidence = min(1.0, max(0.0, (peak_val - valley) / max(peak_val, 0.01)))

    return int(round(wpm)), float(confidence)


def estimate_wpm_fast(audio, sample_rate=12000, tone_freq=600, window_sec=10.0):
    """Estimate WPM via element duration clustering.

    CW isn't periodic at the dit period — it has dits, dahs, and variable
    spaces. Autocorrelation doesn't work. Instead:
    1. Extract keying envelope (bandpass + rectify + lowpass)
    2. Find mark/space transitions
    3. Cluster mark durations into short (dit) and long (dah) groups
    4. Dit duration → WPM = 1200 / dit_ms
    """
    from scipy.signal import butter, filtfilt

    n = min(len(audio), int(window_sec * sample_rate))
    if n < sample_rate * 0.5:
        return 0, 0.0

    audio = audio[:n].astype(np.float32)

    # Bandpass around tone_freq ± 150 Hz
    nyq = sample_rate / 2
    lo = max(10, tone_freq - 150) / nyq
    hi = min(nyq - 10, tone_freq + 150) / nyq
    if lo >= hi:
        return 0, 0.0
    b, a = butter(3, [lo, hi], btype='band')
    filtered = filtfilt(b, a, audio)

    # Full-wave rectify → lowpass at 30 Hz → clean keying envelope
    rectified = np.abs(filtered)
    lp_freq = min(30.0, nyq - 10) / nyq
    b_lp, a_lp = butter(2, lp_freq, btype='low')
    smoothed = filtfilt(b_lp, a_lp, rectified)

    peak = np.max(smoothed)
    if peak < 1e-10:
        return 0, 0.0
    smoothed = smoothed / peak

    # Threshold: low to capture full mark duration (filter rounds edges)
    threshold = np.median(smoothed) + 0.15 * (np.max(smoothed) - np.median(smoothed))
    binary = smoothed > threshold

    # Find transitions → extract mark durations
    transitions = np.diff(binary.astype(np.int8))
    mark_starts = np.where(transitions == 1)[0]
    mark_ends = np.where(transitions == -1)[0]

    # Align: ensure we have matching start/end pairs
    if len(mark_ends) > 0 and (len(mark_starts) == 0 or mark_ends[0] < mark_starts[0]):
        mark_ends = mark_ends[1:]  # discard partial first mark
    n_marks = min(len(mark_starts), len(mark_ends))
    if n_marks < 3:
        return 0, 0.0

    mark_durations = (mark_ends[:n_marks] - mark_starts[:n_marks]).astype(np.float64)

    # Convert to milliseconds
    mark_ms = mark_durations * 1000.0 / sample_rate

    # Filter out very short (noise spikes < 10ms) and very long (stuck key > 2000ms)
    mark_ms = mark_ms[(mark_ms > 10) & (mark_ms < 2000)]
    if len(mark_ms) < 3:
        return 0, 0.0

    # Cluster into dits and dahs using the midpoint of sorted durations
    # The gap between dit cluster and dah cluster is typically at 2× dit
    sorted_ms = np.sort(mark_ms)
    median = np.median(sorted_ms)

    # Split at 2× the shortest cluster center (approximate)
    # First estimate: dits are below median, dahs above
    dits = sorted_ms[sorted_ms < median]
    dahs = sorted_ms[sorted_ms >= median]

    if len(dits) >= 2:
        dit_avg = np.median(dits)
    elif len(dahs) >= 2:
        # All elements might be dahs (slow CW with long elements)
        dit_avg = np.median(dahs) / 3.0
    else:
        dit_avg = median

    # Refine: re-cluster with threshold at 2× dit
    threshold_ms = dit_avg * 2.0
    dits = mark_ms[mark_ms < threshold_ms]
    dahs = mark_ms[mark_ms >= threshold_ms]

    # Prefer dah-derived dit estimate (dahs are longer, less affected by
    # envelope filter edge clipping which systematically shortens marks)
    if len(dahs) >= 2 and len(dits) >= 2:
        dit_from_dah = np.median(dahs) / 3.0
        dit_from_dit = np.median(dits)
        # Use dah-derived if ratio is reasonable (2-5×), else use dit directly
        ratio = np.median(dahs) / np.median(dits)
        if 2.0 < ratio < 5.0:
            dit_ms = dit_from_dah  # more reliable
        else:
            dit_ms = (dit_from_dah + dit_from_dit) / 2.0
    elif len(dahs) >= 2:
        dit_ms = np.median(dahs) / 3.0
    elif len(dits) >= 2:
        dit_ms = np.median(dits)
    else:
        dit_ms = dit_avg

    if dit_ms < 15:  # > 80 WPM, unrealistic
        return 0, 0.0

    wpm = 1200.0 / dit_ms
    wpm = max(5.0, min(60.0, wpm))

    # Confidence: ratio of dits to total marks (more elements = more confident)
    # Also check dit/dah ratio is near 1:3
    confidence = min(1.0, n_marks / 20.0)  # scale with number of elements observed
    if len(dits) >= 2 and len(dahs) >= 2:
        ratio = np.median(dahs) / np.median(dits)
        if 2.0 < ratio < 4.5:
            confidence = min(1.0, confidence * 1.2)  # good ratio boosts confidence
        else:
            confidence *= 0.5  # bad ratio reduces confidence

    return int(round(wpm)), float(min(1.0, confidence))


if __name__ == '__main__':
    import sys
    import wave

    if len(sys.argv) < 2:
        print("Usage: wpm_estimator.py <wav_file> [tone_freq]")
        sys.exit(1)

    w = wave.open(sys.argv[1], 'rb')
    sr = w.getframerate()
    samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    w.close()

    freq = int(sys.argv[2]) if len(sys.argv) > 2 else 600

    wpm, conf = estimate_wpm_fast(samples, sr, freq)
    print(f"WPM:{wpm} confidence:{conf:.2f}")
