"""Both HPSDR receivers tune only the receivers they were given (on a radio shared through hpsdr_proxy.py the
others are someone else's: 10-27 a listener decoding RX2 tuned RX1 to 7000 kHz), and the C receiver can
receive on a port of its own (hpsdr_listen_port), so it can share a computer with the proxy."""
import ctypes as ct
import socket
import time
from pathlib import Path

import pytest

from hpsdr_receiver import HPSDRReceiver

_LIB = Path(__file__).resolve().parent.parent / "libhpsdr_fast.so"


def _udp(timeout=0.3):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(timeout)
    return s


def _rx_freqs(data):
    """{receiver (0-based): Hz} from a C&C packet's NCO registers (C0 0x04..0x10 = RX1..RX7, 0x24 = RX8)."""
    out = {}
    for off in (8, 520):
        if data[off:off + 3] == b"\x7f\x7f\x7f":
            c0 = data[off + 3] & 0xFE
            rx = 7 if c0 == 0x24 else c0 // 2 - 2 if 0x04 <= c0 <= 0x10 else None
            if rx is not None:
                out[rx] = int.from_bytes(data[off + 4:off + 8], "big")
    return out


def _free_port():
    s = _udp()
    port = s.getsockname()[1]
    s.close()
    return port


def _heard(radio, seconds=1.5):
    """{receiver: Hz} from every C&C packet the fake radio gets, and the source ports they came from."""
    freqs, ports = {}, set()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data, addr = radio.recvfrom(2048)
        except socket.timeout:
            continue
        ports.add(addr[1])
        if data[2:4] == b"\x01\x02":
            freqs.update(_rx_freqs(data))
    return freqs, ports


def test_the_python_receiver_tunes_only_its_own_receiver() -> None:
    radio = _udp()
    rx = HPSDRReceiver("127.0.0.1", port=radio.getsockname()[1], n_receivers=4, sample_rate=192000,
                       listen_port=_free_port())
    try:
        rx.set_frequency(1, 14_090_000)
        rx.start(quiet=True)
        freqs, _ = _heard(radio, 0.8)
        assert list(freqs) == [1]                          # RX2 only: RX1, RX3 and RX4 untouched
        assert abs(freqs[1] - 14_090_000) < 100            # (the -3.9 ppm calibration)
    finally:
        rx.sock.close()
        radio.close()


@pytest.mark.skipif(not _LIB.exists(), reason="libhpsdr_fast.so not built")
def test_the_c_receiver_tunes_only_its_own_and_listens_on_its_own_port() -> None:
    lib = ct.CDLL(str(_LIB))
    lib.hpsdr_create_listen.restype = ct.c_void_p
    lib.hpsdr_create_listen.argtypes = [ct.c_char_p, ct.c_int, ct.c_int, ct.c_int, ct.c_int, ct.c_int]
    for f in ("hpsdr_start", "hpsdr_stop", "hpsdr_destroy"):
        getattr(lib, f).argtypes = [ct.c_void_p]
    lib.hpsdr_set_freq.argtypes = [ct.c_void_p, ct.c_int, ct.c_uint32]
    radio = _udp()
    listen = _free_port()
    h = lib.hpsdr_create_listen(b"127.0.0.1", radio.getsockname()[1], listen, 4, 192000, 20)
    assert h
    try:
        lib.hpsdr_set_freq(h, 2, 18_083_000)
        lib.hpsdr_set_freq(h, 3, 21_090_000)
        lib.hpsdr_start(h)
        freqs, ports = _heard(radio)
        assert sorted(freqs) == [2, 3]                     # RX3 and RX4 only
        assert ports == {listen}                           # sent from its own port, not the radio's
    finally:
        lib.hpsdr_stop(h)
        lib.hpsdr_destroy(h)
        radio.close()
