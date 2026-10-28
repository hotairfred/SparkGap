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


def test_track_rules() -> None:
    hits = [(1.0, 7_190_000, 'LSB', 9.0), (3.0, 7_190_200, 'LSB', 9.0), (5.0, 7_189_900, 'LSB', 9.0),
            (7.0, 7_195_000, 'LSB', 9.0),                                  # one hit: no track
            (9.0, 7_180_000, 'LSB', 9.0), (29.0, 7_180_000, 'LSB', 9.0),   # two hits 20 s apart: no track
            (0.0, 7_170_000, 'LSB', 9.0), (100.0, 7_170_000, 'LSB', 9.0), (200.0, 7_170_000, 'LSB', 9.0),
            (300.0, 7_160_000, 'LSB', 9.0), (302.0, 7_160_000, 'LSB', 9.0),
            (430.0, 7_160_000, 'LSB', 9.0), (432.0, 7_160_000, 'LSB', 9.0)]   # 128 s quiet: two tracks
    tracks = {(round(t['carrier_hz'] / 1000), t['start']): t for t in vd.build_tracks(hits)}
    assert sorted(tracks) == [(7160, 298.0), (7160, 428.0), (7170, 0.0), (7190, 0.0)]
    tr = tracks[(7190, 0.0)]
    assert tr['sb'] == 'LSB' and tr['hits'] == 3 and abs(tr['carrier_hz'] - 7_190_000) <= 100
    assert tr['start'] == max(0.0, 1.0 - vd.PAD_S) and tr['end'] == 5.0 + vd.PAD_S
    assert tracks[(7170, 0.0)]['hits'] == 3        # 3 hits minutes apart count (a sporadic voice, YO7WC)


def tone_file(tmp_path, offset_hz, seconds=7, name='tone.cf32'):
    t = np.arange(seconds * FS) / FS
    iq = (0.1 * np.exp(2j * np.pi * offset_hz * t)).astype(np.complex64)
    path = str(tmp_path / name)
    np.stack([iq.real, iq.imag], 1).astype(np.float32).tofile(path)
    return path, iq


def cut(path, centre, track, start, end, tmp_path):
    got = {}
    vd.cut_audio_fft(path, centre, [track], start, end, lambda k, a: got.setdefault(k, a), tmp_dir=str(tmp_path))
    return got[0]


def test_the_cut_audio_is_the_voice_and_joins_across_blocks(tmp_path, monkeypatch) -> None:
    from scipy.signal import hilbert
    monkeypatch.setattr(vd, 'BLOCK_S', 2)                               # several blocks in a short file
    centre, carrier = 7_200_000, 7_189_950          # not a whole bin of any block: the case that stepped
    path, _ = tone_file(tmp_path, carrier - 1_000 - centre)           # LSB: 1 kHz audio
    a = cut(path, centre, {'sb': 'LSB', 'carrier_hz': carrier, 'start': 0.5, 'end': 6.5}, 0.0, 7.0, tmp_path)
    assert abs(len(a) - 6.0 * vd.AUDIO_FS) <= vd.AUDIO_FS * 0.01
    spec = np.abs(np.fft.rfft(a))
    assert abs(np.fft.rfftfreq(len(a), 1 / vd.AUDIO_FS)[np.argmax(spec)] - 1_000) <= 5
    ph = np.unwrap(np.angle(hilbert(a)))[200:-200]
    step = np.diff(ph) - np.median(np.diff(ph))
    assert np.abs(step).max() < 0.05                                    # no phase step at the block joins
    assert not list(tmp_path.glob('vd_cut_*'))                          # temp files cleaned up


def test_a_track_from_the_start_to_past_the_end_of_the_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(vd, 'BLOCK_S', 2)
    centre, carrier = 7_200_000, 7_190_000
    path, _ = tone_file(tmp_path, carrier - 1_000 - centre, seconds=5)
    a = cut(path, centre, {'sb': 'LSB', 'carrier_hz': carrier, 'start': 0.0, 'end': 6.0}, 0.0, 5.0, tmp_path)
    assert abs(len(a) - 5.0 * vd.AUDIO_FS) <= vd.AUDIO_FS * 0.01        # up to the end of the file


def test_temp_files_go_when_emit_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(vd, 'BLOCK_S', 2)
    path, _ = tone_file(tmp_path, -11_000, seconds=3)

    def boom(k, a):
        raise RuntimeError('disk full')
    try:
        vd.cut_audio_fft(path, 7_200_000, [{'sb': 'LSB', 'carrier_hz': 7_190_000, 'start': 0.0, 'end': 3.0}],
                         0.0, 3.0, boom, tmp_dir=str(tmp_path))
    except RuntimeError:
        pass
    assert not list(tmp_path.glob('vd_cut_*'))


def test_wav_iq_16_and_24_bit(tmp_path) -> None:
    import wave
    _, iq = tone_file(tmp_path, 5_000, seconds=1)
    for bits in (16, 24):
        scale = (1 << (bits - 1)) - 1
        v = np.round(np.stack([iq.real, iq.imag], 1).ravel() * scale).astype(np.int32)
        raw = v.astype('<i2').tobytes() if bits == 16 else b''.join(
            int(x).to_bytes(3, 'little', signed=True) for x in v)
        path = str(tmp_path / ('iq%d.wav' % bits))
        with wave.open(path, 'wb') as w:
            w.setnchannels(2)
            w.setsampwidth(bits // 8)
            w.setframerate(FS)
            w.writeframes(raw)
        assert vd.total_seconds(path) == 1.0
        got = vd.read_block(path, 0.25, 0.5)
        assert len(got) == FS // 2
        assert np.abs(got - iq[FS // 4: FS * 3 // 4]).max() < 2.0 / scale
        assert len(vd.read_block(path, 2.0, 1.0)) == 0                  # past the end: nothing, no error
    with wave.open(str(tmp_path / 'mono.wav'), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(FS)
        w.writeframes(b'\0\0' * 100)
    try:
        vd.read_block(str(tmp_path / 'mono.wav'), 0.0, 0.001)
        raise AssertionError('a mono WAV must be refused')
    except ValueError:
        pass


def test_60m_is_usb_and_40m_lsb() -> None:
    assert vd.usb(14_200_000) and vd.usb(5_357_000) and vd.usb(5_403_500)
    assert not vd.usb(7_200_000) and not vd.usb(3_750_000) and not vd.usb(1_850_000)


def test_thresholds_come_from_the_environment_only_when_asked(monkeypatch) -> None:
    monkeypatch.setenv('VD_SHAPE_DB', '9.5')
    assert vd.SHAPE_DB == 4.0                                # importing read nothing
    monkeypatch.setattr(vd, 'SHAPE_DB', vd.SHAPE_DB)         # restored after the test
    monkeypatch.setattr(vd, 'END_S', vd.END_S)
    monkeypatch.setenv('VD_END_S', '60')
    vd.settings_from_env()
    assert vd.SHAPE_DB == 9.5 and vd.END_S == 60.0
