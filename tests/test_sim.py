"""tools/sim: composer frequency/level mapping and truth-scorer categories."""
import json
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(HERE, 'tools', 'sim')
sys.path.insert(0, SIM)
import compose  # noqa: E402


def _cell(d, name, pitch, dur=2.0, rate=12000):
    t = np.arange(int(dur * rate)) / rate
    x = (0.4 * np.sin(2 * np.pi * pitch * t) * 32767).astype('<i2')
    w = wave.open(os.path.join(d, name + '.wav'), 'wb')
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(x.tobytes()); w.close()
    json.dump({'format': 'sparkgap-cell/1', 'sample_rate': rate, 'duration_s': dur,
               'stations': [{'call': 'K1ABC', 'role': 'runner', 'pitch_hz': pitch, 'level_db': 0}],
               'messages': [{'t0': 0.1, 't1': 1.0, 'call': 'K1ABC', 'kind': 'cq', 'text': 'CQ K1ABC'}]},
              open(os.path.join(d, name + '.json'), 'w'))


def test_compose_places_tone_at_rf_and_level():
    with tempfile.TemporaryDirectory() as d:
        _cell(d, 'c0', 600.0)
        json.dump({'center_khz': 7030.0, 'rate': 192000, 'duration_s': 2.0, 'noise_dbfs': None,
                   'cells': [{'base': 'c0', 'rf_khz': 7010.0, 'level_dbfs': -20.0}]},
                  open(os.path.join(d, 'scene.json'), 'w'))
        subprocess.run([sys.executable, os.path.join(SIM, 'compose.py'),
                        os.path.join(d, 'scene.json'), os.path.join(d, 'out')],
                       check=True, capture_output=True)
        w = wave.open(os.path.join(d, 'out_7030kHz_192000_24bit.wav'))
        raw = np.frombuffer(w.readframes(w.getnframes()), np.uint8).reshape(-1, 3)
        v = (raw[:, 0].astype(np.int32) | raw[:, 1].astype(np.int32) << 8 | raw[:, 2].astype(np.int32) << 16)
        v = np.where(v >= 2 ** 23, v - 2 ** 24, v).astype(np.float64) / 2 ** 23
        z = v[0::2] + 1j * v[1::2]
        spec = np.abs(np.fft.fft(z[48000:144000]))
        f = np.fft.fftfreq(96000, 1 / 192000)[np.argmax(spec)]
        assert abs(f - (7010600 - 7030000)) < 5            # -19400 Hz, positive-frequency side only
        assert abs(20 * np.log10(np.abs(z[48000:144000]).max()) + 20.0) < 0.5
        tr = json.load(open(os.path.join(d, 'out.truth.json')))
        assert tr['stations'][0]['rf_hz'] == 7010600.0 and tr['messages'][0]['rf_hz'] == 7010600.0


def test_score_truth_categories():
    truth = {'stations': [{'call': 'K1ABC', 'role': 'runner', 'rf_hz': 7010600.0},
                          {'call': 'W2XYZ', 'role': 'caller', 'rf_hz': 7010700.0}]}
    log = ('SPOT: 7010.6 kHz K1ABC 20 dB\nSPOT: 7010.7 kHz W2XYZ 15 dB\n'
           'SPOT: 7015.0 kHz K1ABC 10 dB\nSPOT: 7020.0 kHz EE5E 9 dB\n')
    with tempfile.TemporaryDirectory() as d:
        json.dump(truth, open(os.path.join(d, 't.json'), 'w'))
        open(os.path.join(d, 'l.log'), 'w').write(log)
        out = subprocess.run([sys.executable, os.path.join(SIM, 'score_truth.py'),
                              os.path.join(d, 'l.log'), os.path.join(d, 't.json')],
                             check=True, capture_output=True, text=True).stdout
    assert 'runner recall   1/1' in out
    assert 'RUNNER         1' in out and 'CALLER         1' in out and 'BUST           1' in out
    assert 'WRONG_FREQ     0' in out          # K1ABC's off-frequency spot doesn't demote it
