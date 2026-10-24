"""The single-band Python HPSDR receiver sends the start sequence again after restart_after_s without
IQ, as the C receiver does (hotairfred/SparkGap#92): while IQ flows it never does, 0 turns it off,
and passive mode (riding along on someone else's stream) never sends anything."""
import socket
import struct
import threading
import time

import hpsdr_receiver as hr


def _port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _packet(seq: int) -> bytes:
    frame = b"\x7f\x7f\x7f" + b"\0" * 5 + b"\0" * 504
    return b"\xef\xfe\x01\x06" + struct.pack(">I", seq) + frame * 2


def _run(restart_after_s: float, passive: bool = False):
    port = _port()
    rx = hr.HPSDRReceiver("127.0.0.1", port=port, n_receivers=1, sample_rate=192000, passive=passive)
    rx.restart_after_s = restart_after_s
    got = []
    t = threading.Thread(target=rx.receive, args=(lambda i, iq: got.append(len(iq)),), kwargs={"duration": 3.0})
    return rx, port, got, t


def _send(port: int, seconds: float) -> None:
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    end, n = time.monotonic() + seconds, 0
    while time.monotonic() < end:
        tx.sendto(_packet(n), ("127.0.0.1", port))
        n += 1
        time.sleep(0.005)
    tx.close()


def test_restarts_after_silence_and_not_while_streaming() -> None:
    rx, port, got, t = _run(0.5)
    t.start()
    _send(port, 0.8)                         # streaming: no restart
    assert rx.restarts == 0 and got
    time.sleep(0.8)                          # quiet past 0.5 s: at least one restart
    assert rx.restarts >= 1
    n = rx.restarts
    _send(port, 0.8)                         # streaming again: no more
    assert rx.restarts == n
    t.join()
    rx.sock.close()


def test_zero_and_passive_never_restart() -> None:
    for after, passive in ((0, False), (0.3, True)):
        rx, port, _got, t = _run(after, passive)
        t.start()
        time.sleep(1.2)
        assert rx.restarts == 0
        t.join()
        rx.sock.close()


def test_a_restart_prints_one_line_in_the_c_receivers_wording(capsys) -> None:
    rx, port, _got, t = _run(0.4)
    t.start()
    t.join()
    rx.sock.close()
    err = capsys.readouterr().err
    lines = [l for l in err.splitlines() if l.strip()]
    assert rx.restarts >= 2
    assert all(l.startswith("Receiver: no IQ from 127.0.0.1, sent the start sequence again") for l in lines), lines
