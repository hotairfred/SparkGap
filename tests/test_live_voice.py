"""live_voice.py without a GPU or a radio: the live rules on transcripts, the telnet port, the transcriber's
failure stop, and each track's audio across blocks."""
import argparse
import os
import socket
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'voice'))
import live_voice as lv  # noqa: E402


def runner(port=0, **kw):
    a = argparse.Namespace(centre=7200.0, lo=7107.0, hi=7293.0, port=port, call='N0CALL-4', spots=None,
                           fcc=None, transcripts=None, no_lockon=False, batch=0)
    for k, v in kw.items():
        setattr(a, k, v)
    return lv.Runner(a)


def cq(t, call_words):
    return [[t, t + 3.0, 'CQ contest, CQ contest, %s, contest.' % call_words, -0.2, []]]


def test_the_live_rule_spots_a_runner_once_then_again_after_10_minutes() -> None:
    r = runner()
    out = []
    r.emit = out.append
    words = 'Kilo One Alpha Bravo Charlie'
    for t in (0.0, 30.0, 60.0):
        r.rule(7190.0, t, cq(t, words))
    assert [g['call'] for g in out] == ['K1ABC']
    r.rule(7192.0, 90.0, cq(90.0, words))                     # the same voice 2 kHz up: no second spot
    r.rule(7192.0, 120.0, cq(120.0, words))
    assert len(out) == 1
    for t in (700.0, 730.0):
        r.rule(7190.0, t, cq(t, words))
    assert [g['call'] for g in out] == ['K1ABC', 'K1ABC']     # re-spotted after RESPOT_S


def test_the_telnet_port_greets_and_sends_dx_lines() -> None:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    r = runner(port=port)
    c = socket.create_connection(('127.0.0.1', port), timeout=5)
    assert b'call' in c.recv(100)
    c.sendall(b'N0TEST\r\n')
    assert b'N0CALL-4' in c.recv(200)
    for _ in range(50):                                       # the server adds the client after the greeting
        if r.telnet.clients:
            break
        time.sleep(0.02)
    line = r.telnet.spot(7189.9, 'K1ABC', 'SSB CQ 2 copies')
    got = c.recv(200).decode()
    c.close()
    assert got.strip() == line
    assert line.startswith('DX de N0CALL-4:') and '7189.9' in line and 'K1ABC' in line and line.endswith('Z')


class Failing:
    def transcribe(self, audio, **kw):
        raise RuntimeError('Library libcublas.so.12 is not found or cannot be loaded')


class Silent:
    def transcribe(self, audio, **kw):
        return iter(()), None                                 # a silent piece: no speech, no GPU needed


def test_the_transcriber_stops_loudly_when_every_piece_fails(monkeypatch) -> None:
    r = runner()
    why = []
    r.fatal = why.append
    audio = np.zeros(lv.AUDIO_FS, np.float32)
    for k in range(lv.FAIL_STOP + 5):
        r.q.put((1, -k, k, (7190000.0, 'LSB', float(k), k + 1.0, audio)))
    r.whisper_loop(Failing(), {})
    assert why and 'in a row' in why[0] and r.fail_run == lv.FAIL_STOP


def test_silent_pieces_do_not_hide_a_broken_transcriber() -> None:
    r = runner()
    why = []
    r.fatal = why.append
    audio = np.zeros(lv.AUDIO_FS, np.float32)
    n = 0
    for k in range(3 * lv.FAIL_STOP):                         # failing pieces with silent ones between
        n += 1
        r.q.put((1, -n, n, (7190000.0, 'LSB', float(n), n + 1.0, audio)))

    class Mixed:
        calls = 0

        def transcribe(self, audio, **kw):
            Mixed.calls += 1
            if Mixed.calls % 3 == 0:
                return iter(()), None
            raise RuntimeError('cuda error')
    r.whisper_loop(Mixed(), {})
    assert why                                                # silent "successes" did not reset the count


def test_a_tracks_audio_is_continuous_across_blocks() -> None:
    from scipy.signal import hilbert
    r = runner()
    fs, centre, carrier = lv.FS, 7_200_000.0, 7_189_950.0     # not a whole bin: the awkward case
    tr = lv.Track('LSB', carrier, 0.0)
    n_blk = int(lv.BLOCK_S * fs)
    parts = []
    for b in range(4):
        t = np.arange(b * n_blk, (b + 1) * n_blk) / fs
        iq = (0.1 * np.exp(2j * np.pi * (carrier - 1_000 - centre) * t)).astype(np.complex64)   # 1 kHz audio
        parts.append(r.mix(tr, r.spectrum(b * lv.BLOCK_S, iq)))
    # the audio runs MIX_PAD_S late, so its first quarter second is from before the file: skip 0.5 s each end
    a = lv.sideband_audio(np.concatenate(parts), 'LSB')[lv.AUDIO_FS // 2:-lv.AUDIO_FS // 2]
    peak = np.fft.rfftfreq(len(a), 1 / lv.AUDIO_FS)[np.argmax(np.abs(np.fft.rfft(a)))]
    assert abs(peak - 1_000) <= 5
    ph = np.unwrap(np.angle(hilbert(a)))[200:-200]
    assert np.abs(np.diff(ph) - np.median(np.diff(ph))).max() < 0.05


def _free_tcp_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_a_silent_client_does_not_hold_up_the_next() -> None:
    port = _free_tcp_port()
    lv.Telnet(port, 'N0CALL-4')
    silent = socket.create_connection(('127.0.0.1', port), timeout=5)     # connects, never answers
    t0 = time.monotonic()
    c = socket.create_connection(('127.0.0.1', port), timeout=5)
    assert b'call' in c.recv(100)
    c.sendall(b'N0TEST\r\n')
    assert b'N0CALL-4' in c.recv(200)
    assert time.monotonic() - t0 < 2                                    # not 30 s behind the silent one
    silent.close()
    c.close()


def test_a_client_that_stops_reading_is_dropped_without_stalling_spots(monkeypatch) -> None:
    monkeypatch.setattr(lv, 'SEND_TIMEOUT_S', 0.3)
    port = _free_tcp_port()
    tel = lv.Telnet(port, 'N0CALL-4')
    c = socket.create_connection(('127.0.0.1', port), timeout=5)
    c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    c.recv(100)
    c.sendall(b'N0TEST\r\n')
    c.recv(200)
    for _ in range(50):
        if tel.clients:
            break
        time.sleep(0.02)
    slowest = 0.0
    for k in range(200_000):                                            # it never reads again
        t0 = time.monotonic()
        tel.spot(7189.9, 'K1ABC', 'SSB CQ 2 copies ' + 'x' * 30)
        slowest = max(slowest, time.monotonic() - t0)
        if not tel.clients:
            break
    assert not tel.clients                                              # dropped
    assert slowest < 1.0                                                # each spot() came back within the timeout
    c.close()
