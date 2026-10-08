#!/usr/bin/env python3
"""Band composer: place sparkgap-cell/1 cells across one receiver's span and
write 192 kHz 24-bit I/Q (same convention as the skimmer1 all-band recorder,
replay with `hpsdr_proxy --negate-q`, or run SparkGap file mode directly) plus a
truth file in absolute RF.

Scene file (JSON):
{
  "center_khz": 7030.0,             receiver centre
  "rate": 192000,
  "duration_s": 300,                 cells are cut/padded to this
  "noise_dbfs": -40,                 rms of the complex band noise over the whole span
  "seed": 1,
  "cells": [
    {"base": "cells/k1abc", "rf_khz": 7021.4, "level_dbfs": -30},
    ...                              rf_khz = the cell's dial: audio pitch p -> rf_khz + p/1000
  ]
}
Each cell is normalised so its loudest station's keyed level (99.9th percentile of
the envelope) sits at level_dbfs.

usage: compose.py SCENE.json OUT_BASENAME
    -> OUT_BASENAME_<centre>kHz_192000_24bit.wav + OUT_BASENAME.truth.json
"""
import json
import math
import os
import sys
import wave

import numpy as np
from scipy.signal import hilbert, resample_poly


def read_cell_audio(path):
    w = wave.open(path, 'rb')
    rate, sw, ch, n = w.getframerate(), w.getsampwidth(), w.getnchannels(), w.getnframes()
    raw = w.readframes(n)
    w.close()
    if sw == 2:
        x = np.frombuffer(raw, '<i2').astype(np.float32) / 32768.0
    elif sw == 4:
        x = np.frombuffer(raw, '<f4').astype(np.float32)
    else:
        raise SystemExit(f'{path}: unsupported sample width {sw}')
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, rate


def cell_to_band(x, rate, out_rate, n_out, offset_hz):
    """Real USB audio -> analytic -> out_rate complex, shifted by offset_hz."""
    z = hilbert(x.astype(np.float64)).astype(np.complex64)
    g = math.gcd(int(out_rate), int(rate))
    z = resample_poly(z, out_rate // g, rate // g).astype(np.complex64)
    z = z[:n_out] if len(z) >= n_out else np.pad(z, (0, n_out - len(z)))
    t = np.arange(n_out, dtype=np.float64) / out_rate
    return z * np.exp(2j * np.pi * offset_hz * t).astype(np.complex64)


def write_iq24(path, z, rate):
    full = 2 ** 23 - 1
    v = np.empty(2 * len(z), np.int32)
    v[0::2] = np.clip(np.rint(z.real * full), -full - 1, full)
    v[1::2] = np.clip(np.rint(z.imag * full), -full - 1, full)
    b = v.astype('<i4').view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    w = wave.open(path, 'wb')
    w.setnchannels(2); w.setsampwidth(3); w.setframerate(rate)
    w.writeframes(b)
    w.close()


def main():
    scene_path, out = sys.argv[1], sys.argv[2]
    sc = json.load(open(scene_path))
    base_dir = os.path.dirname(os.path.abspath(scene_path))
    rate, dur = int(sc.get('rate', 192000)), float(sc['duration_s'])
    centre = float(sc['center_khz']) * 1e3
    n = int(dur * rate)
    rng = np.random.default_rng(sc.get('seed', 1))
    band = np.zeros(n, np.complex64)
    truth = dict(format='sparkgap-scene/1', center_hz=centre, rate=rate, duration_s=dur,
                 noise_dbfs=sc.get('noise_dbfs'), scene=os.path.basename(scene_path),
                 stations=[], messages=[])
    for ci, c in enumerate(sc['cells']):
        cb = c['base'] if os.path.isabs(c['base']) else os.path.join(base_dir, c['base'])
        meta = json.load(open(cb + '.json'))
        x, r = read_cell_audio(cb + '.wav')
        dial = float(c['rf_khz']) * 1e3
        z = cell_to_band(x, r, rate, n, dial - centre)
        # robust peak: the analytic/resample step rings briefly at the file edges
        peak = float(np.percentile(np.abs(z), 99.9)) or 1.0
        z *= np.float32(10 ** (c['level_dbfs'] / 20.0) / peak)
        band += z
        for s in meta['stations']:
            truth['stations'].append(dict(call=s['call'], role=s['role'], cell=ci,
                                          rf_hz=round(dial + s['pitch_hz'], 1),
                                          level_dbfs=round(c['level_dbfs'] + s.get('level_db', 0), 1),
                                          wpm=s.get('wpm')))
        pitch = {s['call']: s['pitch_hz'] for s in meta['stations']}
        for m in meta['messages']:
            if m['t0'] < dur:
                truth['messages'].append(dict(m, cell=ci, rf_hz=round(dial + pitch[m['call']], 1)))
        print(f'cell {ci}: {os.path.basename(cb)} at {dial / 1e3:.2f} kHz, {c["level_dbfs"]} dBFS, '
              f'{len(meta["stations"])} stations', file=sys.stderr)
    if sc.get('noise_dbfs') is not None:
        sigma = 10 ** (sc['noise_dbfs'] / 20.0) / math.sqrt(2.0)
        band += (rng.standard_normal(n, dtype=np.float32) +
                 1j * rng.standard_normal(n, dtype=np.float32)) * np.float32(sigma)
    pk = float(np.abs(band).max())
    if pk > 1.0:
        print(f'WARNING: peak {pk:.2f} > full scale; lower cell levels', file=sys.stderr)
    wav = f'{out}_{centre / 1e3:.0f}kHz_{rate}_24bit.wav'
    write_iq24(wav, band, rate)
    truth['messages'].sort(key=lambda m: m['t0'])
    json.dump(truth, open(out + '.truth.json', 'w'), indent=1)
    print(f'{wav}: {len(sc["cells"])} cells, {len(truth["stations"])} stations, '
          f'{len(truth["messages"])} messages, peak {20 * math.log10(max(pk, 1e-12)):.1f} dBFS')


if __name__ == '__main__':
    main()
