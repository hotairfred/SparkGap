#!/usr/bin/env python3
"""Cut a CW clip to listen to from an IQ recording: a frequency and a time in, a mono WAV out with
the signal at a comfortable pitch, plus what SparkGap's log says it decoded there.

Listening tells a signal problem (QSB, a rough fist, QRM, a collision) from a decoder problem,
which the decoded text alone can't.

  cw_clip.py RECORDING --freq 7031.2 --time 13:05:20 [--dur 40] [--log sparkgap.log]

RECORDING is a recorder WAV (YYYYMMDD_HHMMSSZ_<centre>kHz_<rate>_24bit.wav, continued in _p2,
_p3 ... files), a directory of them (the band whose centre is nearest --freq), or any stereo IQ WAV
with --centre KHZ and --start 'YYYY-MM-DD HH:MM:SS' (UTC). The sample rate comes from the header.
SparkGap logs local time: --log-utc-offset (default this PC's) says how far the log's PC is from UTC.
--time is UTC (HH:MM or HH:MM:SS on the recording's day) or +SECONDS from the recording's start.
Writes clip_<freq>_<time>.wav (8 kHz, 16 bit) and, with --log, the matching decoder lines beside
it as .txt. --bw 2400 gives a wide receiver passband instead of a CW filter.
"""
import argparse
import calendar
import glob
import os
import re
import sys
import time
import wave

import numpy as np

OUT_RATE = 8000
_NAME = re.compile(r'(\d{8})_(\d{6})Z_(\d+)kHz_(\d+)_(\d+)bit(?:_p(\d+))?\.wav$')
# a frequency in kHz in a log line ("ITILA raw 7031.2 kHz", "*** SPOT:     7031.2  K9US")
_LOG_FREQ = re.compile(r'(?<![\d.])(\d{3,5}\.\d)(?![\d.])')
_LOG_TIME = re.compile(r'[0-2]\d:[0-5]\d:[0-5]\d [A-Z]')
_LOG_KINDS = ('ITILA raw', '*** SPOT', '*** LOCAL', 'cost-gate', 'RTTY ')


def parse_name(path):
    """(start epoch, centre Hz, rate, part number) from a recorder file name, or None."""
    m = _NAME.search(os.path.basename(path))
    if not m:
        return None
    day, hms, ck, rate, _bits, part = m.groups()
    t0 = calendar.timegm(time.strptime(day + hms, '%Y%m%d%H%M%S'))
    return t0, int(ck) * 1000, int(rate), int(part or 1)


def recording_parts(path, freq_hz=None, centre_khz=None, start=None):
    """(parts in order, start epoch, centre Hz, rate). A directory: the band whose centre is nearest
    freq_hz. A WAV not named by the recorder (record_wav's template, e.g. lake3_14038_20261021_183854Z.wav)
    needs centre_khz and start ('YYYY-MM-DD HH:MM:SS' UTC); the rate always comes from its header."""
    if os.path.isdir(path):
        firsts = [p for p in glob.glob(os.path.join(path, '*.wav'))
                  if parse_name(p) and parse_name(p)[3] == 1]
        if not firsts:
            raise SystemExit(f"no recorder WAVs in {path}")
        path = min(firsts, key=lambda p: abs(parse_name(p)[1] - freq_hz))
    with wave.open(path) as w:
        rate = w.getframerate()
    info = parse_name(path)
    if info is None:
        if centre_khz is None or start is None:
            raise SystemExit(f"{path}: not a recorder file name (YYYYMMDD_HHMMSSZ_<kHz>kHz_...wav), so give "
                             "--centre KHZ and --start 'YYYY-MM-DD HH:MM:SS' (UTC)")
        t0 = calendar.timegm(time.strptime(start, '%Y-%m-%d %H:%M:%S'))
        return [path], t0, int(round(centre_khz * 1000)), rate
    t0, ck = info[0], info[1]
    if centre_khz is not None:
        ck = int(round(centre_khz * 1000))
    if start is not None:
        t0 = calendar.timegm(time.strptime(start, '%Y-%m-%d %H:%M:%S'))
    base = re.sub(r'_p\d+\.wav$', '.wav', path)
    parts = [base] + sorted(glob.glob(base[:-4] + '_p*.wav'), key=lambda p: parse_name(p)[3])
    return parts, t0, ck, rate


def _decode(raw, sw):
    """Interleaved PCM -> float32 (n, 2) in +-1."""
    if sw == 3:
        a = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        v = a[:, 2].astype(np.int32) << 16 | a[:, 1].astype(np.int32) << 8 | a[:, 0].astype(np.int32)
        v[v >= 1 << 23] -= 1 << 24
        return (v.astype(np.float32) / 8388608.0).reshape(-1, 2)
    dt = {2: np.int16, 4: np.int32}[sw]
    return (np.frombuffer(raw, dt).astype(np.float32) / float(1 << (8 * sw - 1))).reshape(-1, 2)


def read_iq(parts, first, count):
    """count frames from frame `first` of the recording, across its part files: complex64."""
    out, pos = [], 0
    for p in parts:
        with wave.open(p, 'rb') as w:
            n = w.getnframes()
            if first < pos + n and count > 0:
                start = max(0, first - pos)
                k = min(count, n - start)
                w.setpos(start)
                s = _decode(w.readframes(k), w.getsampwidth())
                out.append(s[:, 0] + 1j * s[:, 1])
                count -= len(s)
                first = pos + n
            pos += n
    if not out:
        raise SystemExit("that time is past the end of the recording")
    return np.concatenate(out).astype(np.complex64)


def cw_audio(iq, rate, offset_hz, pitch=600.0, bw=500.0, out_rate=OUT_RATE):
    """The real audio of [offset_hz - bw/2, offset_hz + bw/2] of iq, offset_hz at `pitch` Hz:
    one FFT, the slice moved down with soft (raised cosine) edges, a short inverse FFT."""
    n = len(iq)
    z = np.fft.fft(iq)
    n_out = n * out_rate // rate
    bin_hz = rate / n
    half = int(bw / 2 / bin_hz)
    edge = max(1, int(min(50.0, bw / 4) / bin_hz))           # 50 Hz skirts: no ringing clicks
    k0 = int(round(offset_hz / bin_hz))
    kp = int(round(pitch / bin_hz))
    idx = np.arange(-half, half + 1)
    w = np.ones(len(idx))
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(edge) / edge)
    w[:edge], w[-edge:] = ramp, ramp[::-1]
    sp = np.zeros(n_out, dtype=np.complex128)
    sp[(idx + kp) % n_out] = z[(idx + k0) % n] * w
    return np.fft.ifft(sp).real * (n_out / n)


def normalise(audio):
    """16-bit PCM at a level that suits the ear: the 99.9th percentile at -3 dBFS, so one burst of
    QRM doesn't leave the rest silent; what is louder clips."""
    ref = np.percentile(np.abs(audio), 99.9)
    if ref <= 0:
        return np.zeros(len(audio), np.int16)
    return (np.clip(audio / ref * 0.7, -1.0, 1.0) * 32767).astype(np.int16)


def parse_time(s, rec_t0):
    """Epoch of --time: +SECONDS from the start, or HH:MM[:SS] UTC on the recording's day (the
    next day if that is before the recording started)."""
    if s.startswith('+'):
        return rec_t0 + float(s[1:])
    hms = [int(x) for x in s.split(':')] + [0]
    day0 = rec_t0 - rec_t0 % 86400
    t = day0 + hms[0] * 3600 + hms[1] * 60 + hms[2]
    return t + 86400 if t < rec_t0 - 60 else t


def log_lines(log_path, freq_khz, t_from, t_to, tol_khz=0.15, utc_offset_h=0.0):
    """Decoder lines of a SparkGap log near freq_khz from t_from to t_to (epochs). The log's HH:MM:SS
    are the logging PC's local time, utc_offset_h hours ahead of UTC (SparkGap logs %(asctime)s),
    matched by time of day, so give it the log of that day. ITILA reports a window up to its length
    after the audio, so callers pass a t_to past the clip's end."""
    shift = utc_offset_h * 3600.0
    lo, hi = (t_from + shift) % 86400, (t_to + shift) % 86400
    out, keeping = [], False
    with open(log_path, errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')
            if not _LOG_TIME.match(line):
                if keeping:                    # decoded text that ran onto the next line
                    out[-1] += '\n' + line
                continue
            keeping = False
            if not any(k in line for k in _LOG_KINDS):
                continue
            tod = int(line[0:2]) * 3600 + int(line[3:5]) * 60 + int(line[6:8])
            if not (lo <= tod <= hi if lo <= hi else (tod >= lo or tod <= hi)):
                continue
            fm = _LOG_FREQ.search(line, 9)
            if fm and abs(float(fm.group(1)) - freq_khz) <= tol_khz:
                out.append(line)
                keeping = True
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('recording', help='recorder WAV, a directory of them, or any IQ WAV with --centre and --start')
    ap.add_argument('--freq', type=float, required=True, help='signal frequency, kHz')
    ap.add_argument('--time', required=True, help='UTC HH:MM[:SS], or +SECONDS into the recording')
    ap.add_argument('--dur', type=float, default=40.0, help='clip length, s (default 40)')
    ap.add_argument('--pitch', type=float, default=600.0, help='CW pitch, Hz (default 600)')
    ap.add_argument('--bw', type=float, default=500.0, help='passband, Hz (default 500)')
    ap.add_argument('--log', help='SparkGap log of the same time: its decoder lines go in a .txt')
    ap.add_argument('--log-after', type=float, default=70.0,
                    help='also take log lines this many s after the clip (ITILA reports late)')
    ap.add_argument('--out', help='output WAV (default clip_<freq>_<HHMMSS>.wav)')
    ap.add_argument('--centre', type=float, help='receiver centre, kHz (a WAV not named by the recorder)')
    ap.add_argument('--start', help="recording start, 'YYYY-MM-DD HH:MM:SS' UTC (a WAV not named by the recorder)")
    ap.add_argument('--log-utc-offset', type=float, default=time.localtime().tm_gmtoff / 3600.0,
                    help="hours the log's PC runs ahead of UTC (SparkGap logs local time); default this "
                         "PC's, so give it for a log from a PC in another zone (0 for a UTC one)")
    a = ap.parse_args(argv)

    parts, rec_t0, ck, rate = recording_parts(a.recording, a.freq * 1000, a.centre, a.start)
    offset = a.freq * 1000 - ck
    if abs(offset) > rate / 2 - a.bw:
        raise SystemExit(f"{a.freq} kHz is outside {os.path.basename(parts[0])} "
                         f"({(ck - rate / 2) / 1000:.1f}-{(ck + rate / 2) / 1000:.1f} kHz)")
    t = parse_time(a.time, rec_t0)
    first = int(round((t - rec_t0) * rate))
    if first < 0:
        raise SystemExit("that time is before the recording starts")
    iq = read_iq(parts, first, int(a.dur * rate))
    audio = normalise(cw_audio(iq, rate, offset, a.pitch, a.bw))
    stamp = time.strftime('%H%M%S', time.gmtime(t))
    out = a.out or f'clip_{a.freq:.1f}_{stamp}.wav'
    with wave.open(out, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(audio.tobytes())
    print(f"{out}: {len(audio) / OUT_RATE:.1f} s of {a.freq:.1f} kHz from "
          f"{time.strftime('%H:%M:%S', time.gmtime(t))}Z, {os.path.basename(parts[0])}, "
          f"pitch {a.pitch:.0f} Hz, {a.bw:.0f} Hz wide")
    if a.log:
        lines = log_lines(a.log, a.freq, t, t + len(audio) / OUT_RATE + a.log_after, utc_offset_h=a.log_utc_offset)
        txt = os.path.splitext(out)[0] + '.txt'
        with open(txt, 'w') as f:
            f.write(f"# {a.freq:.1f} kHz {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(t))}Z "
                    f"+{a.dur:.0f} s; decoder lines within 0.15 kHz from {a.log}\n")
            f.writelines(line + '\n' for line in lines)
        print(f"{txt}: {len(lines)} decoder lines")
        for line in lines[:40]:
            print('  ' + line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
