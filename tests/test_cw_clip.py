"""tools/listen/cw_clip.py: a tone in a recording comes out at the pitch, across part files, and
the log lines near the frequency and time come with it."""
import os
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "listen"))
import cw_clip  # noqa: E402

RATE = 48000                 # small recordings: the tool takes the rate from the file name


def _write(path, iq):
    v = np.round(np.stack([iq.real, iq.imag], 1).ravel() * 8388607).astype('<i4')
    raw = v.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE); w.writeframes(raw)


def _recording(d, secs=6, tone_hz=7031200.0, ck_khz=7020, split=2.5):
    """A recording centred at ck_khz with one tone, in two part files split `split` s in."""
    t = np.arange(int(secs * RATE)) / RATE
    iq = 0.1 * np.exp(2j * np.pi * (tone_hz - ck_khz * 1000) * t)
    base = d / f"20261014_125900Z_{ck_khz}kHz_{RATE}_24bit.wav"
    k = int(split * RATE)
    _write(base, iq[:k])
    _write(d / f"20261014_125900Z_{ck_khz}kHz_{RATE}_24bit_p2.wav", iq[k:])
    return base


def _peak_hz(path):
    with wave.open(str(path)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(float)
    sp = np.abs(np.fft.rfft(a))
    return np.fft.rfftfreq(len(a), 1 / cw_clip.OUT_RATE)[np.argmax(sp)], len(a)


def test_the_tone_comes_out_at_the_pitch_across_part_files(tmp_path):
    _recording(tmp_path)
    out = tmp_path / "c.wav"
    # from 1.5 s for 2 s: crosses into the _p2 file at 2.5 s; a directory picks the band
    cw_clip.main([str(tmp_path), "--freq", "7031.2", "--time", "+1.5",
                  "--dur", "2", "--out", str(out)])
    hz, n = _peak_hz(out)
    assert abs(hz - 600) < 2 and n == 2 * cw_clip.OUT_RATE
    cw_clip.main([str(tmp_path), "--freq", "7031.2", "--time", "+1.5", "--dur", "2",
                  "--pitch", "800", "--out", str(out)])
    assert abs(_peak_hz(out)[0] - 800) < 2


def test_another_frequency_is_quiet(tmp_path):
    base = _recording(tmp_path)
    parts, t0, ck, rate = cw_clip.recording_parts(str(base))
    assert [p.endswith("_p2.wav") for p in parts] == [False, True] and rate == RATE
    iq = cw_clip.read_iq(parts, RATE, 3 * RATE)
    on = cw_clip.cw_audio(iq, rate, 7031200.0 - ck)
    mirror = cw_clip.cw_audio(iq, rate, -(7031200.0 - ck))
    assert np.sqrt(np.mean(mirror ** 2)) < 1e-3 * np.sqrt(np.mean(on ** 2))


def test_time_of_day_and_offsets():
    t0 = cw_clip.parse_name("20261014_235900Z_7090kHz_192000_24bit.wav")[0]
    assert cw_clip.parse_time("+30", t0) == t0 + 30
    assert cw_clip.parse_time("23:59:10", t0) == t0 + 10
    assert cw_clip.parse_time("00:01", t0) == t0 + 120          # after midnight: the next day


def test_log_lines_near_the_frequency_and_time_with_their_continuations(tmp_path):
    log = tmp_path / "sparkgap.log"
    log.write_text(
        "13:07:40 INFO ITILA raw 7035.0 kHz cost=0.2: 'CQ CWT W3RJ\n"
        "W3RJ'\n"
        "13:07:41 INFO ITILA raw 7035.4 kHz cost=0.2: 'too far off'\n"
        "13:07:53 INFO *** SPOT:     7035.1  W3RJ          42 dB  28 WPM  [repeat] ***\n"
        "13:07:54 INFO Health: bins=200 7035.0\n"
        "13:20:00 INFO ITILA raw 7035.0 kHz cost=0.2: 'later'\n")
    t0 = cw_clip.parse_name("20261014_125900Z_7090kHz_192000_24bit.wav")[0]
    got = cw_clip.log_lines(str(log), 7035.0, t0 + 8 * 60, t0 + 10 * 60)
    assert got == ["13:07:40 INFO ITILA raw 7035.0 kHz cost=0.2: 'CQ CWT W3RJ\nW3RJ'",
                   "13:07:53 INFO *** SPOT:     7035.1  W3RJ          42 dB  28 WPM  [repeat] ***"]


def test_any_iq_wav_with_centre_and_start(tmp_path):
    base = _recording(tmp_path)
    other = tmp_path / "lake3_7020_20261014_125900Z.wav"          # record_wav's template, not the recorder's
    os.rename(base, other)
    out = tmp_path / "c.wav"
    import pytest
    with pytest.raises(SystemExit):
        cw_clip.main([str(other), "--freq", "7031.2", "--time", "+1", "--out", str(out)])
    cw_clip.main([str(other), "--freq", "7031.2", "--time", "12:59:01", "--dur", "1",
                  "--centre", "7020", "--start", "2026-10-14 12:59:00", "--out", str(out)])
    assert abs(_peak_hz(out)[0] - 600) < 2


def test_log_times_are_the_logging_pcs_local_time(tmp_path):
    log = tmp_path / "sparkgap.log"
    log.write_text("09:07:53 INFO *** SPOT:     7035.1  W3RJ          42 dB  28 WPM  [repeat] ***\n")
    t0 = cw_clip.parse_name("20261014_125900Z_7090kHz_192000_24bit.wav")[0]     # 12:59Z
    assert cw_clip.log_lines(str(log), 7035.0, t0 + 8 * 60, t0 + 10 * 60, utc_offset_h=-4)   # EDT log
    assert not cw_clip.log_lines(str(log), 7035.0, t0 + 8 * 60, t0 + 10 * 60, utc_offset_h=0)
