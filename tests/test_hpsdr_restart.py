"""With no IQ for restart_after_s the C receiver sends the start sequence again, so a radio that
another client stopped streams again (2026-10-22: a Red Pitaya stopped by another program on the
LAN sent nothing for 11 hours). While IQ flows it never restarts; 0 turns it off."""
import ctypes as ct
import socket
import struct
import time
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "libhpsdr_fast.so"
if not _LIB.exists():
    pytest.skip("libhpsdr_fast.so not built", allow_module_level=True)


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _packet(seq: int) -> bytes:
    frame = b"\x7f\x7f\x7f" + b"\0" * 5 + b"\0" * 504
    return b"\xef\xfe\x01\x06" + struct.pack(">I", seq) + frame * 2


@pytest.fixture
def radio():
    lib = ct.CDLL(str(_LIB))
    lib.hpsdr_create.restype = ct.c_void_p
    lib.hpsdr_create.argtypes = [ct.c_char_p, ct.c_int, ct.c_int, ct.c_int, ct.c_int]
    for f in ("hpsdr_start", "hpsdr_stop", "hpsdr_destroy"):
        getattr(lib, f).argtypes = [ct.c_void_p]
    lib.hpsdr_set_freq.argtypes = [ct.c_void_p, ct.c_int, ct.c_uint32]
    lib.hpsdr_set_restart_after.argtypes = [ct.c_void_p, ct.c_double]
    lib.hpsdr_restarts.restype = ct.c_uint64
    lib.hpsdr_restarts.argtypes = [ct.c_void_p]
    lib.hpsdr_pkt_count.restype = ct.c_uint64
    lib.hpsdr_pkt_count.argtypes = [ct.c_void_p]
    port = _free_udp_port()
    h = lib.hpsdr_create(b"127.0.0.1", port, 1, 192000, 20)
    assert h
    lib.hpsdr_set_freq(h, 0, 14_074_000)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = [0]

    def stream(seconds: float, rate: float = 200.0) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            tx.sendto(_packet(seq[0]), ("127.0.0.1", port))
            seq[0] += 1
            time.sleep(1 / rate)

    yield lib, h, stream
    lib.hpsdr_stop(h)
    lib.hpsdr_destroy(h)
    tx.close()


def test_default_is_30_seconds_and_quiet_while_streaming(radio):
    lib, h, stream = radio
    lib.hpsdr_start(h)
    stream(1.0)
    assert lib.hpsdr_pkt_count(h) > 0
    assert lib.hpsdr_restarts(h) == 0


def _wait_for(fn, value, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end and fn() < value:
        time.sleep(0.02)
    return fn()


def test_restarts_after_silence_then_again_while_silent(radio):
    lib, h, stream = radio
    lib.hpsdr_set_restart_after(h, 0.5)
    lib.hpsdr_start(h)
    stream(1.0)                       # IQ flowing: no restart
    assert lib.hpsdr_restarts(h) == 0
    t0 = time.monotonic()
    assert _wait_for(lambda: lib.hpsdr_restarts(h), 1, 2.0) == 1     # quiet past 0.5 s: a restart
    assert time.monotonic() - t0 >= 0.45
    assert _wait_for(lambda: lib.hpsdr_restarts(h), 2, 2.0) == 2     # still quiet: it tries again
    time.sleep(0.4)                   # let the second start sequence finish sending
    n = lib.hpsdr_restarts(h)
    before = lib.hpsdr_pkt_count(h)
    stream(1.5)                       # the radio is back: counted, and no more restarts
    assert lib.hpsdr_pkt_count(h) > before
    assert lib.hpsdr_restarts(h) == n


def test_zero_turns_it_off(radio):
    lib, h, stream = radio
    lib.hpsdr_set_restart_after(h, 0)
    lib.hpsdr_start(h)
    stream(0.3)
    time.sleep(1.0)
    assert lib.hpsdr_restarts(h) == 0
