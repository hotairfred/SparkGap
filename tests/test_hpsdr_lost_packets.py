"""Lost EP6 packets are counted from the sequence number and their time is kept as zeros, so an FT8
snapshot's length matches the time it spans (hotairfred/SparkGap#80).  Duplicates and late packets
are dropped; a jump of more than a second resyncs without filling."""
import ctypes as ct
import socket
import struct
import time
from pathlib import Path

import numpy as np
import pytest

_LIB = Path(__file__).resolve().parent.parent / "libhpsdr_fast.so"
if not _LIB.exists():
    pytest.skip("libhpsdr_fast.so not built", allow_module_level=True)

SPP = 2 * (504 // 8)        # samples per packet with one receiver: 2 frames of 63 groups


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _frame(i_val: int) -> bytes:
    """One 512-byte USB frame: sync, 5 control bytes, 63 groups of 24-bit I, Q and 16-bit mic."""
    group = i_val.to_bytes(3, "big", signed=True) + (1000).to_bytes(3, "big", signed=True) + b"\0\0"
    return b"\x7f\x7f\x7f" + b"\0" * 5 + group * 63 + b"\0" * (504 - 63 * 8)


def _packet(seq: int) -> bytes:
    # I carries seq % 1000 + 1, so every real sample is non-zero and we can see which packet it came from
    return b"\xef\xfe\x01\x06" + struct.pack(">I", seq) + _frame(seq % 1000 + 1) * 2


@pytest.fixture
def radio():
    lib = ct.CDLL(str(_LIB))
    lib.hpsdr_create.restype = ct.c_void_p
    lib.hpsdr_create.argtypes = [ct.c_char_p, ct.c_int, ct.c_int, ct.c_int, ct.c_int]
    for f in ("hpsdr_start", "hpsdr_stop", "hpsdr_destroy"):
        getattr(lib, f).argtypes = [ct.c_void_p]
    lib.hpsdr_set_freq.argtypes = [ct.c_void_p, ct.c_int, ct.c_uint32]
    lib.hpsdr_enable_ft8.argtypes = [ct.c_void_p, ct.c_int, ct.c_double, ct.c_double]
    lib.hpsdr_pkt_lost.restype = ct.c_uint64
    lib.hpsdr_pkt_lost.argtypes = [ct.c_void_p]
    lib.hpsdr_ft8_swap_read.argtypes = [ct.c_void_p, ct.c_int, ct.c_void_p, ct.c_void_p, ct.c_int,
                                        ct.POINTER(ct.c_double)]
    port = _free_udp_port()
    h = lib.hpsdr_create(b"127.0.0.1", port, 1, 192000, 20)
    assert h
    lib.hpsdr_set_freq(h, 0, 14_074_000)
    lib.hpsdr_enable_ft8(h, 0, 14_074_000.0, 14_074_000.0)
    lib.hpsdr_start(h)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(seqs: list[int]) -> None:
        for s in seqs:
            tx.sendto(_packet(s), ("127.0.0.1", port))
            time.sleep(0.0005)
        time.sleep(0.2)

    def snapshot() -> np.ndarray:
        i = np.zeros(192000 * 2, np.float32)
        q = np.zeros_like(i)
        t = ct.c_double()
        n = lib.hpsdr_ft8_swap_read(h, 0, i.ctypes.data, q.ctypes.data, len(i), ct.byref(t))
        return i[:n]

    yield lib, h, send, snapshot
    tx.close()
    lib.hpsdr_stop(h)
    lib.hpsdr_destroy(h)


def test_a_gap_is_counted_and_filled_with_zeros(radio) -> None:
    lib, h, send, snapshot = radio
    send(list(range(0, 10)) + list(range(15, 20)))       # packets 10..14 lost
    assert lib.hpsdr_pkt_lost(h) == 5
    i = snapshot()
    assert len(i) == 20 * SPP                            # 15 real packets + 5 of zeros
    assert np.all(i[10 * SPP:15 * SPP] == 0)
    assert np.all(i[:10 * SPP] != 0) and np.all(i[15 * SPP:] != 0)
    assert i[15 * SPP] == 16 and i[10 * SPP - 1] == 10    # each packet sits in its own place


def test_duplicates_and_late_packets_are_dropped(radio) -> None:
    lib, h, send, snapshot = radio
    send([0, 1, 2, 2, 1, 3])
    assert lib.hpsdr_pkt_lost(h) == 0
    assert len(snapshot()) == 4 * SPP


def test_the_sequence_wraps(radio) -> None:
    lib, h, send, snapshot = radio
    send([0xFFFF_FFFE, 0xFFFF_FFFF, 1])                  # packet 0 lost across the wrap
    assert lib.hpsdr_pkt_lost(h) == 1
    assert len(snapshot()) == 4 * SPP


def test_a_long_jump_resyncs_without_filling(radio) -> None:
    lib, h, send, snapshot = radio
    send([0, 1, 100_000, 100_001])                       # over a second ahead: the radio restarted
    assert lib.hpsdr_pkt_lost(h) == 0
    assert len(snapshot()) == 4 * SPP
