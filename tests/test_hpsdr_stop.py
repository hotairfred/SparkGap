"""hpsdr_stop returns when the radio has gone quiet (hotairfred/SparkGap#49): the receive loop
must not block in recv() forever once running is cleared."""
import ctypes as ct
import socket
import threading
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


def test_stop_returns_with_no_packets_arriving() -> None:
    lib = ct.CDLL(str(_LIB))
    lib.hpsdr_create.restype = ct.c_void_p
    lib.hpsdr_create.argtypes = [ct.c_char_p, ct.c_int, ct.c_int, ct.c_int, ct.c_int]
    lib.hpsdr_start.argtypes = [ct.c_void_p]
    lib.hpsdr_stop.argtypes = [ct.c_void_p]
    lib.hpsdr_destroy.argtypes = [ct.c_void_p]
    # a "radio" at a loopback port nobody answers on: no IQ will ever arrive
    h = lib.hpsdr_create(b"127.0.0.1", _free_udp_port(), 1, 192000, 20)
    assert h
    lib.hpsdr_start(h)
    time.sleep(0.3)
    t = threading.Thread(target=lib.hpsdr_stop, args=(h,), daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(3.0)
    assert not t.is_alive(), "hpsdr_stop hung waiting for a packet"
    assert time.monotonic() - t0 < 1.0
    lib.hpsdr_destroy(h)
