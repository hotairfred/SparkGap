"""voice_detect.py on synthetic IQ: a voice is found at its carrier and on its sideband, a CW carrier and a
steady noise band (RTTY, data) are not, tracks follow the spawn rules, and the cutter's audio is the
voice's, continuous across blocks."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'voice'))
import voice_detect as vd  # noqa: E402

FS = vd.FS
TOL_HZ = 400        # the carrier from the voice's shape sits 0-300 Hz on its quiet side (live_voice.py moves spots
                    # CARRIER_OFFSET_HZ back); within JOIN_HZ is what the tracker needs


def voice(n, offset_hz, sb, rng, level=0.002):
    """Speech-like SSB at offset_hz from the centre: harmonics of a wandering 110-140 Hz pitch from the
    fundamental up to 2600 Hz (formant-shaped), in syllables of 120-300 ms with pauses between."""
    t = np.arange(n) / FS
    f0 = 125 + 15 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(f0) / FS
    gate = np.zeros(n)
    k = 0
    while k < n:
        on = int(rng.uniform(0.12, 0.30) * FS)
        gate[k:k + on] = 1.0
        k += on + int(rng.uniform(0.08, 0.25) * FS)
    z = np.zeros(n, np.complex128)
    sign = 1 if sb == 'USB' else -1
    for h in range(1, 22):
        hz = 125 * h
        if hz > 2600:
            continue
        amp = np.exp(-((hz - 700) / 600) ** 2) + 0.6 * np.exp(-((hz - 1800) / 500) ** 2)
        z += amp * np.exp(1j * sign * h * phase)
    return level * gate * z * np.exp(2j * np.pi * offset_hz * t)


def noise(n, rng, level=0.001):
    return level * (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2)


def hits_for(iq, centre):
    lo, hi = centre - 20000, centre + 20000
    db, f = vd.spectra(iq.astype(np.complex64), centre, lo, hi)
    return vd.window_hits(db, f, vd.floor_db(db), lo, hi, 0.0)


def test_an_lsb_voice_is_found_at_its_carrier() -> None:
    rng = np.random.default_rng(1)
    n, centre = 12 * FS, 7_200_000
    iq = noise(n, rng) + voice(n, -10_000, 'LSB', rng)
    hits = hits_for(iq, centre)
    near = [h for h in hits if abs(h[1] - (centre - 10_000)) <= TOL_HZ]
    assert len(near) >= 4                              # most of the six 2 s windows
    assert all(h[2] == 'LSB' for h in near)            # 40 m: LSB by convention
    assert not [h for h in hits if abs(h[1] - (centre - 10_000)) > 3000]


def test_a_usb_voice_on_20m() -> None:
    rng = np.random.default_rng(2)
    n, centre = 12 * FS, 14_200_000
    iq = noise(n, rng) + voice(n, 5_000, 'USB', rng)
    near = [h for h in hits_for(iq, centre) if abs(h[1] - (centre + 5_000)) <= TOL_HZ]
    assert len(near) >= 4 and all(h[2] == 'USB' for h in near)


def test_a_cw_carrier_and_a_steady_noise_band_are_not_voices() -> None:
    rng = np.random.default_rng(3)
    n, centre = 12 * FS, 7_200_000
    t = np.arange(n) / FS
    cw = 0.002 * np.exp(2j * np.pi * -8_000 * t)                       # a carrier: one bin, no rhythm
    band = np.fft.fft(rng.standard_normal(n) + 1j * rng.standard_normal(n))
    fr = np.fft.fftfreq(n, 1 / FS)
    band[(fr < 6_000) | (fr > 8_400)] = 0                              # 2.4 kHz of steady data-like noise
    band = 0.002 * np.fft.ifft(band) / np.std(np.fft.ifft(band))
    assert hits_for(noise(n, rng) + cw + band, centre) == []


def test_tracks_need_two_hits_in_10_s_and_join_within_400_hz() -> None:
    hits = [(1.0, 7_190_000, 'LSB', 9.0), (3.0, 7_190_200, 'LSB', 9.0), (5.0, 7_189_900, 'LSB', 9.0),
            (7.0, 7_195_000, 'LSB', 9.0),                              # one hit: no track
            (9.0, 7_180_000, 'LSB', 9.0), (29.0, 7_180_000, 'LSB', 9.0)]   # two hits 20 s apart: no track
    tracks = vd.build_tracks(hits)
    assert len(tracks) == 1
    tr = tracks[0]
    assert tr['sb'] == 'LSB' and tr['hits'] == 3 and abs(tr['carrier_hz'] - 7_190_000) <= 100
    assert tr['start'] == max(0.0, 1.0 - vd.PAD_S) and tr['end'] == 5.0 + vd.PAD_S


def test_the_cut_audio_is_the_voice_and_joins_across_blocks(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(vd, 'BLOCK_S', 2)                               # several blocks in a short file
    monkeypatch.setenv('VD_TMP', str(tmp_path))                         # the cutter's temp files go here
    n, centre, carrier = 7 * FS, 7_200_000, 7_190_000
    t = np.arange(n) / FS
    iq = (0.1 * np.exp(2j * np.pi * (carrier - 1_000 - centre) * t)).astype(np.complex64)   # LSB: 1 kHz audio
    path = str(tmp_path / 'tone.cf32')
    np.stack([iq.real, iq.imag], 1).astype(np.float32).tofile(path)
    got = {}
    vd.cut_audio_fft(path, centre, [{'sb': 'LSB', 'carrier_hz': carrier, 'start': 0.5, 'end': 6.5}],
                     0.0, 7.0, lambda k, a: got.setdefault(k, a))
    a = got[0]
    assert abs(len(a) - 6.0 * vd.AUDIO_FS) <= vd.AUDIO_FS * 0.01
    spec = np.abs(np.fft.rfft(a))
    peak = np.fft.rfftfreq(len(a), 1 / vd.AUDIO_FS)[np.argmax(spec)]
    assert abs(peak - 1_000) <= 5
    rms = np.sqrt((a[: len(a) // 1600 * 1600].reshape(-1, 1600) ** 2).mean(1))   # 0.1 s pieces
    assert rms.min() > 0.9 * rms.max()                                  # no gap or step at block edges
    assert not list(tmp_path.glob('vd_cut_*'))                          # and are cleaned up
