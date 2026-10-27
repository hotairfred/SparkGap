"""SSB voice tracks from wideband IQ: find each voice by its filter shape and speech rhythm,
follow it, and cut its audio for the transcriber (voice_spot.py reads the transcripts).

usage: voice_detect.py IQ CENTRE_KHZ LO_KHZ HI_KHZ OUTDIR [--start S] [--dur S] [--tracks-only]
IQ at 192 kHz: .cf32 (interleaved float32 I, Q; measured on FlexRadio DAX-IQ recordings) or a stereo 16- or
24-bit WAV as record_wav and iq_recorder.py write them (read, but not yet measured on voice; a Red Pitaya
recording may need Q negated, as hpsdr_proxy.py --negate-q does). LSB below 10 MHz, USB above.
Writes OUTDIR/tracks.json and OUTDIR/clips/ssb_<carrier kHz>@<start s>.wav (16 kHz, one per track).

Detection, every 2 s window (25 Hz bins, 40 ms frames):
  shape   for each candidate carrier c (50 Hz grid): USB = mean dB over [c+300, c+2600] minus mean over
          [c-500, c+100]; LSB mirrored. A voice's filter leaves a full block on one side, nothing on
          the carrier side.
  spread  >= 30% of the block's bins 6 dB over the floor (a CW carrier or birdie fills one bin)
  rhythm  the block's 40 ms power swings >= 3 dB (std of dB): syllables and pauses; carriers, RTTY,
          FT8 and noise stay flat
Tracks: a hit within 400 Hz of a live track (same sideband) extends it; a track ends after 120 s with no
hit and is kept with 3 hits or more, or with 2 within 10 s (a sporadic voice with 3 hits minutes apart
counts: YO7WC on 15 m in CQ WW came only from one).
"""
import json
import os
import sys
import wave

import numpy as np

FS = 192000
AUDIO_FS = 16000
FRAME = 7680                     # 40 ms, 25 Hz bins
WIN_FRAMES = 50                  # 2 s windows
BLOCK_S = 60
GRID_HZ = 50
BOX = (300, 2600)                # voice block relative to the carrier
GUARD = (-500, 100)              # carrier side, should be quiet
SHAPE_DB = float(os.environ.get('VD_SHAPE_DB', 4.0))
LEVEL_DB = float(os.environ.get('VD_LEVEL_DB', 6.0))
SPREAD = float(os.environ.get('VD_SPREAD', 0.30))
RHYTHM_DB = float(os.environ.get('VD_RHYTHM_DB', 3.0))
JOIN_HZ = 400
NMS_HZ = 2200                    # one voice per 2.2 kHz of a sideband in each window (10-27: letting closer
                                 # neighbours through added transcription and junk, not calls)
CARRIER_DB = 10.0               # a peak this far over the block at the carrier = AM or a carrier, not SSB
SPAWN_HITS, SPAWN_S = 2, 10
END_S = float(os.environ.get('VD_END_S', 120))
PAD_S = 2


def read_block(path, start_s, dur_s):
    """IQ from start_s for dur_s (whole samples, rounded; shorter at the end of the file)."""
    n0, n = int(round(start_s * FS)), int(round(dur_s * FS))
    if path.endswith('.cf32'):
        a = np.fromfile(path, dtype=np.float32, offset=n0 * 8, count=n * 2)
        return (a[0::2] + 1j * a[1::2]).astype(np.complex64)
    with wave.open(path, 'rb') as w:
        width = w.getsampwidth()
        if w.getnchannels() != 2 or w.getframerate() != FS or width not in (2, 3):
            raise ValueError('%s: need stereo 16- or 24-bit IQ at %d Hz, got %d channels, %d bits, %d Hz'
                             % (path, FS, w.getnchannels(), 8 * width, w.getframerate()))
        if n0 >= w.getnframes():
            return np.zeros(0, np.complex64)
        w.setpos(n0)
        raw = w.readframes(n)
    if width == 2:
        v = np.frombuffer(raw, dtype='<i2').astype(np.float32) / (1 << 15)
    else:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        v = np.where(v >= 1 << 23, v - (1 << 24), v).astype(np.float32) / (1 << 23)
    v = v.reshape(-1, 2)
    return (v[:, 0] + 1j * v[:, 1]).astype(np.complex64)


def total_seconds(path):
    if path.endswith('.cf32'):
        return os.path.getsize(path) / 8 / FS
    with wave.open(path) as w:
        return w.getnframes() / FS


def spectra(iq, centre_hz, lo_hz, hi_hz):
    """Power (dB) per 40 ms frame and 25 Hz bin over [lo, hi]: (frames, bins), bin frequencies."""
    n = len(iq) // FRAME
    x = iq[:n * FRAME].reshape(n, FRAME) * np.hanning(FRAME).astype(np.float32)
    p = np.abs(np.fft.fftshift(np.fft.fft(x, axis=1), axes=1)) ** 2
    f = np.fft.fftshift(np.fft.fftfreq(FRAME, 1 / FS)) + centre_hz
    keep = (f >= lo_hz - 3000) & (f <= hi_hz + 3000)
    return 10 * np.log10(p[:, keep] + 1e-20), f[keep]


def floor_db(db):
    """Noise floor per bin: the 20th percentile over frequency (10 kHz) of each bin's time median."""
    from scipy.ndimage import percentile_filter
    med = np.median(db, axis=0)
    return percentile_filter(med, 20, size=401, mode='nearest')       # 401 bins = 10 kHz


def window_hits(db, f, floor, lo_hz, hi_hz, t0):
    """[(time, carrier_hz, 'USB'/'LSB', shape_db)] for one block, every 2 s window."""
    hits = []
    snr = db - floor
    df = f[1] - f[0]
    cs = np.arange(lo_hz, hi_hz, GRID_HZ)
    idx = lambda hz: np.round((hz - f[0]) / df).astype(int)
    sides = []
    both = (('USB', BOX, GUARD), ('LSB', (-BOX[1], -BOX[0]), (-GUARD[1], -GUARD[0])))
    usb_band = (lo_hz + hi_hz) / 2 >= 10e6                # convention: USB from 10 MHz up, LSB below
    # Only the band's sideband: looking for both makes a mirrored ghost of every voice (its far edge reads
    # as the other sideband's block, about 3 kHz away), transcribed twice.
    for sb, box, guard in [x for x in both if (x[0] == 'USB') == usb_band]:
        k0, k1 = idx(cs - 40), idx(cs + 40)
        b0, b1, g0, g1 = idx(cs + box[0]), idx(cs + box[1]), idx(cs + guard[0]), idx(cs + guard[1])
        ok = (b0 >= 0) & (g0 >= 0) & (b1 <= len(f)) & (g1 <= len(f))
        sides.append((sb, np.clip(b0, 0, len(f)), np.clip(b1, 0, len(f)),
                      np.clip(g0, 0, len(f)), np.clip(g1, 0, len(f)), ok,
                      np.clip(k0, 0, len(f) - 1), np.clip(k1, 1, len(f))))
    for w0 in range(0, snr.shape[0] - WIN_FRAMES + 1, WIN_FRAMES):
        lin = 10 ** (snr[w0:w0 + WIN_FRAMES] / 10)
        avg = 10 * np.log10(lin.mean(0) + 1e-12)                 # 2 s average spectrum, dB over floor
        cum = np.concatenate([[0.0], np.cumsum(avg)])
        cum_on = np.concatenate([[0.0], np.cumsum(avg > LEVEL_DB)])
        cands = []
        for sb, b0, b1, g0, g1, ok, k0, k1 in sides:
            inb = (cum[b1] - cum[b0]) / np.maximum(1, b1 - b0)
            grd = (cum[g1] - cum[g0]) / np.maximum(1, g1 - g0)
            spread = (cum_on[b1] - cum_on[b0]) / np.maximum(1, b1 - b0)
            good = ok & (inb >= LEVEL_DB) & (inb - grd >= SHAPE_DB) & (spread >= SPREAD)
            for k in np.nonzero(good)[0]:
                if avg[k0[k]:k1[k]].max() - inb[k] >= CARRIER_DB:    # SSB suppresses the carrier
                    continue
                env = 10 * np.log10(lin[:, b0[k]:b1[k]].mean(1) + 1e-12)
                if env.std() >= RHYTHM_DB:
                    cands.append((float(inb[k] - grd[k]), float(cs[k]), sb))
        cands.sort(reverse=True)
        taken = []
        for score, c, sb in cands:                                # strongest first, NMS_HZ apart
            if all(abs(c - c2) > NMS_HZ for c2 in taken):
                taken.append(c)
                hits.append((t0 + (w0 + WIN_FRAMES / 2) * FRAME / FS, c, sb, score))
    return hits


def build_tracks(hits):
    live, done = [], []
    for t, c, sb, score in sorted(hits):
        for tr in live:
            if tr['sb'] == sb and abs(np.median(tr['carriers']) - c) <= JOIN_HZ and t - tr['times'][-1] <= END_S:
                tr['times'].append(t)
                tr['carriers'].append(c)
                break
        else:
            live.append({'sb': sb, 'times': [t], 'carriers': [c]})
        still = []
        for tr in live:
            if t - tr['times'][-1] > END_S:
                done.append(tr)
            else:
                still.append(tr)
        live = still
    done += live
    out = []
    for tr in done:
        ts = tr['times']
        if len(ts) < SPAWN_HITS or (len(ts) == SPAWN_HITS and ts[1] - ts[0] > SPAWN_S):
            continue
        out.append({'sb': tr['sb'], 'carrier_hz': float(np.median(tr['carriers'])),
                    'start': max(0.0, ts[0] - PAD_S), 'end': ts[-1] + PAD_S, 'hits': len(ts)})
    return sorted(out, key=lambda x: (x['carrier_hz'], x['start']))


CUT_PAD_S = 0.25                 # cut_audio_fft: overlap each side, discarded (no wrap-around at block edges)


def _norm(a):
    return np.clip(a / (np.percentile(np.abs(a), 99.5) + 1e-12) * 0.5, -1, 1).astype(np.float32)


def cut_audio_fft(path, centre_hz, tracks, start_s, end_s, emit, tmp_dir=None):
    """Phase-continuous LSB/USB audio per track at 16 kHz, in bounded memory (10-24: mixing and resampling
    each track separately held every track at once and ran a 438-track contest hour out of memory, and was
    ~30x slower). One FFT per block, every block the same length (BLOCK_S plus CUT_PAD_S each side, zeros
    past the ends of the file), so a track's frequency is the same whole bin in every block; each track
    takes its voice-band bins (carrier+200..+3000 Hz for USB, -3000..-200 for LSB) into a 16 kHz spectrum
    and one small inverse FFT; the pads are discarded. The mix's phase is referred to absolute sample
    time, so blocks join without a step. Each block's audio is appended to a per-track temp file (in
    tmp_dir, else VD_TMP, else /tmp; removed even on an error); a track is read back, normalised and
    handed to emit(k, audio) as soon as the blocks pass its end."""
    import shutil
    import tempfile
    total = int(round(total_seconds(path) * FS))
    dec = FS // AUDIO_FS
    pad = int(round(CUT_PAD_S * FS))
    blk = int(round(BLOCK_S * FS))
    n = (blk + 2 * pad + dec - 1) // dec * dec           # the same FFT length for every block
    m = n // dec
    tmp = tempfile.mkdtemp(prefix='vd_cut_', dir=tmp_dir or os.environ.get('VD_TMP', '/tmp'))
    try:
        bufs, done = {}, set()      # k -> temp file of float32 audio so far (contest runners: hour-long tracks)
        order = sorted(range(len(tracks)), key=lambda k: tracks[k]['end'])
        s_end = int(round(end_s * FS))
        for s0 in range(int(round(start_s * FS)), s_end, blk):
            b0, b1 = s0 / FS, min(s_end, s0 + blk) / FS
            act = [k for k, tr in enumerate(tracks) if k not in done and tr['start'] < b1 and tr['end'] > b0]
            if act:
                n0 = s0 - pad                                   # sample at x[0]; may be before the file
                x = np.zeros(n, np.complex64)
                r0, r1 = max(0, n0), min(total, n0 + n)
                if r1 > r0:
                    iq = read_block(path, r0 / FS, (r1 - r0) / FS)
                    x[r0 - n0:r0 - n0 + len(iq)] = iq
                Z = np.fft.fft(x)
                skip = pad // dec                               # output samples before b0
                for k in act:
                    tr = tracks[k]
                    kc = int(round((tr['carrier_hz'] - centre_hz) * n / FS))
                    lo, hi = (200, 3000) if tr['sb'] == 'USB' else (-3000, -200)
                    rel = np.arange(int(np.ceil(lo * n / FS)), int(np.floor(hi * n / FS)) + 1)
                    W = np.zeros(m, np.complex64)
                    W[rel % m] = Z[(kc + rel) % n]
                    y = np.fft.ifft(W)
                    y *= np.exp(-2j * np.pi * kc * n0 / n)      # the shift by kc bins, from sample 0, not n0
                    out = np.real(y).astype(np.float32)
                    a0 = skip + int(max(0.0, tr['start'] - b0) * AUDIO_FS)
                    a1 = skip + int((min(b1, tr['end']) - b0) * AUDIO_FS)
                    fn = bufs.setdefault(k, os.path.join(tmp, '%d.f32' % k))
                    with open(fn, 'ab') as fh:
                        out[a0:a1].tofile(fh)
            for k in [k for k in order if k not in done and tracks[k]['end'] <= b1 and k in bufs]:
                emit(k, _norm(np.fromfile(bufs.pop(k), np.float32)))
                done.add(k)
        for k in list(bufs):
            emit(k, _norm(np.fromfile(bufs.pop(k), np.float32)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    path, centre_khz, lo_khz, hi_khz, outdir = sys.argv[1:6]
    arg = lambda k, d: float(sys.argv[sys.argv.index(k) + 1]) if k in sys.argv else d
    centre, lo, hi = float(centre_khz) * 1000, float(lo_khz) * 1000, float(hi_khz) * 1000
    total = total_seconds(path)
    start = arg('--start', 0.0)
    end = min(total, start + arg('--dur', total - start))            # never past the end of the file
    tj = os.path.join(outdir, 'tracks.json')
    saved = json.load(open(tj)) if os.environ.get('VD_REUSE') and os.path.exists(tj) else None
    if saved and saved.get('span') == [start, end]:                     # reuse a finished detection of this span
        hits, tracks = saved['hits'], saved['tracks']
        print('reusing %s: %d tracks' % (tj, len(tracks)), flush=True)
    else:
        hits = []
        for b0 in np.arange(start, end, BLOCK_S):
            b1 = min(end, b0 + BLOCK_S)
            db, f = spectra(read_block(path, b0, b1 - b0), centre, lo, hi)
            hits += window_hits(db, f, floor_db(db), lo, hi, b0)
            print('block %4.0f s: %d hits so far' % (b0, len(hits)), flush=True)
        tracks = build_tracks(hits)
        os.makedirs(outdir, exist_ok=True)
        json.dump({'span': [start, end], 'hits': hits, 'tracks': tracks}, open(tj, 'w'))
    os.makedirs(os.path.join(outdir, 'clips'), exist_ok=True)
    if '--tracks-only' in sys.argv:
        print('%d tracks, %.0f s of audio' % (len(tracks), sum(t['end'] - t['start'] for t in tracks)))
        return
    def emit(k, a):
        tr = tracks[k]
        name = os.path.join(outdir, 'clips', 'ssb_%.1f@%d.wav' % (tr['carrier_hz'] / 1000, tr['start']))
        with wave.open(name, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(AUDIO_FS)
            w.writeframes((a * 32767).astype(np.int16).tobytes())
        print('%s %s %4.0f-%4.0f s, %d hits' % (os.path.basename(name), tr['sb'], tr['start'], tr['end'], tr['hits']), flush=True)

    cut_audio_fft(path, centre, tracks, start, end, emit)
    print('%d tracks' % len(tracks))


if __name__ == '__main__':
    main()
