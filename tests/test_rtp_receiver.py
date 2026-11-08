"""rtp_receiver: radiod-style RTP IQ channels picked by SSRC, both encodings, gaps filled (hotairfred/SparkGap#146)."""

import socket
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from rtp_iq_send import encode, rtp_packet

from rtp_receiver import RtpReceiver, decode, parse_rtp, ssrc_for


def _iq(n: int, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).uniform(-0.9, 0.9, (n, 2))


@pytest.mark.parametrize("enc,tol", [("f32le", 1e-6), ("s16be", 1.0 / 32768)])
def test_encode_decode_round_trip(enc: str, tol: float) -> None:
    iq = _iq(180)
    parsed = parse_rtp(rtp_packet(111, 7, 1234, 14090, encode(iq, enc)))
    assert parsed is not None
    pt, seq, ts, ssrc, payload = parsed
    assert (pt, seq, ts, ssrc) == (111, 7, 1234, 14090)
    assert np.allclose(decode(payload, enc), iq, atol=tol)


def test_parse_skips_csrc_extension_and_padding() -> None:
    body = encode(_iq(10), "f32le")
    hdr = bytes([0x80 | 0x20 | 0x10 | 0x01, 111]) + (5).to_bytes(2, "big") + (9).to_bytes(4, "big") + (7090).to_bytes(4, "big")
    pkt = hdr + b"\0\0\0\1" + b"\xbe\xde\0\1" + b"\0\0\0\0" + body + b"\0\0\3"
    parsed = parse_rtp(pkt)
    assert parsed is not None and parsed[3] == 7090 and parsed[4] == body
    assert parse_rtp(b"\x40" + bytes(15)) is None          # RTP version 1: not ours


def _receiver(**kw) -> RtpReceiver:
    rx = RtpReceiver("127.0.0.1", port=0, **kw)
    rx.set_frequency(0, 7_090_000)
    rx.set_frequency(1, 14_090_000)
    return rx


def test_ssrc_picks_the_band_and_others_are_counted() -> None:
    rx, got = _receiver(), []
    a, b = _iq(180, 1), _iq(180, 2)
    rx.handle(rtp_packet(111, 0, 0, ssrc_for(7_090_000), encode(a, "f32le")), lambda r, x: got.append((r, x)))
    rx.handle(rtp_packet(111, 0, 0, 14090, encode(b, "f32le")), lambda r, x: got.append((r, x)))
    rx.handle(rtp_packet(111, 0, 0, 21090, encode(b, "f32le")), lambda r, x: got.append((r, x)))
    assert [r for r, _ in got] == [0, 1] and rx.other == 1
    assert np.allclose(got[0][1], a, atol=1e-6) and np.allclose(got[1][1], b, atol=1e-6)


def test_gap_is_filled_with_zeros_and_late_packets_dropped() -> None:
    rx, got = _receiver(), []
    cb = lambda r, x: got.append(x)
    p = [encode(_iq(180, k), "f32le") for k in range(4)]
    rx.handle(rtp_packet(111, 10, 0, 14090, p[0]), cb)
    rx.handle(rtp_packet(111, 13, 540, 14090, p[3]), cb)      # 11 and 12 lost: 360 frames
    rx.handle(rtp_packet(111, 12, 360, 14090, p[2]), cb)      # late: dropped
    assert [len(x) for x in got] == [180, 360, 180]
    assert not got[1].any() and rx.lost == 2 and rx.late == 1


def test_receive_over_loopback() -> None:
    rx = _receiver(encoding="s16be")
    rx.start()
    port = rx.sock.getsockname()[1]
    got: list = []
    t = threading.Thread(target=rx.receive, args=(lambda r, x: got.append((r, len(x))),), kwargs={"duration": 3})
    t.start()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for k in range(20):
        s.sendto(rtp_packet(112, k, 360 * k, 7090, encode(_iq(360, k), "s16be")), ("127.0.0.1", port))
    for _ in range(100):
        if len(got) >= 20:
            break
        threading.Event().wait(0.02)
    rx.stop()
    t.join()
    rx.close()
    assert got == [(0, 360)] * 20 and rx.packets == 20 and rx.lost == 0


def test_close_while_receiving_ends_the_loop() -> None:
    """close() from another thread while receive() waits ends receive() quietly (shutdown)."""
    rx = _receiver()
    rx.start()
    t = threading.Thread(target=rx.receive, args=(lambda r, x: None,))
    t.start()
    threading.Event().wait(0.3)
    rx.close()
    t.join(timeout=2)
    assert not t.is_alive()


def test_group_names_hash_as_radiod_does() -> None:
    """radiod hashes a group name to its address unless dns = yes (ka9q-radio make_maddr, FNV-1):
    the same names give the same addresses as UberSDR's copy of the function."""
    from rtp_receiver import radiod_maddr
    for name in ("pcm.local", "hf-status.local", "sparkgap-iq.local"):
        a = radiod_maddr(name)
        assert a.startswith("239.") and a == radiod_maddr(name)
    assert radiod_maddr("pcm.local") != radiod_maddr("hf-status.local")


def test_restarted_stream_is_followed() -> None:
    """radiod restarted: sequence and timestamp start over; the next packet is taken, not dropped as late."""
    rx, got = _receiver(), []
    cb = lambda r, x: got.append(len(x))
    for k in range(5):
        rx.handle(rtp_packet(111, 30000 + k, 50_000_000 + 180 * k, 14090, encode(_iq(180, k), "f32le")), cb)
    rx.handle(rtp_packet(111, 0, 0, 14090, encode(_iq(180, 9), "f32le")), cb)
    rx.handle(rtp_packet(111, 1, 180, 14090, encode(_iq(180, 10), "f32le")), cb)
    assert got == [180] * 7 and rx.late == 0 and rx.resyncs == 1 and rx.lost == 0


def test_late_run_resyncs() -> None:
    """A run of RESYNC_LATE packets that all look late (sequence restarted, timestamp near) is followed."""
    import rtp_receiver as R
    rx, got = _receiver(), []
    rx.handle(rtp_packet(111, 100, 1000, 14090, encode(_iq(180), "f32le")), lambda r, x: got.append(x))
    for k in range(R.RESYNC_LATE + 3):
        rx.handle(rtp_packet(111, k, 1000 + 180 * k, 14090, encode(_iq(180, k), "f32le")), lambda r, x: got.append(x))
    assert rx.resyncs == 1 and rx.late == R.RESYNC_LATE - 1 and len(got) == 1 + 4


def test_wrong_encoding_is_reported(caplog) -> None:
    """s16be packets (360 frames in 1440 bytes) read as f32le: logged once as an error naming the fix."""
    rx = _receiver(encoding="f32le")
    for k in range(3):
        rx.handle(rtp_packet(112, k, 360 * k, 14090, encode(_iq(360, k), "s16be")), lambda r, x: None)
    errs = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errs) == 1 and "looks like s16be" in errs[0].getMessage()


def test_wrong_rate_is_reported(caplog, monkeypatch) -> None:
    import rtp_receiver as R
    rx, clock = _receiver(), [1000.0]
    monkeypatch.setattr(R.time, "time", lambda: clock[0])
    for k in range(int(R.RATE_CHECK_S * 48000 / 180) + 2):     # radiod at 48 kHz, SparkGap set to 192 kHz
        clock[0] = 1000.0 + k * 180 / 48000
        rx.handle(rtp_packet(111, k, 180 * k, 14090, encode(_iq(180, k), "f32le")), lambda r, x: None)
    errs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errs) == 1 and "48000 samples/s" in errs[0]


def test_right_stream_logs_no_error(caplog, monkeypatch) -> None:
    import rtp_receiver as R
    rx, clock = _receiver(), [1000.0]
    monkeypatch.setattr(R.time, "time", lambda: clock[0])
    for k in range(int(R.RATE_CHECK_S * 192000 / 180) + 2):
        clock[0] = 1000.0 + k * 180 / 192000
        rx.handle(rtp_packet(111, k, 180 * k, 14090, encode(_iq(180, k), "f32le")), lambda r, x: None)
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "WARNING")]


def test_stall_is_logged_and_recovery(caplog, monkeypatch) -> None:
    import rtp_receiver as R
    caplog.set_level("INFO", logger="sparkgap")
    rx, clock = _receiver(), [1000.0]
    monkeypatch.setattr(R.time, "time", lambda: clock[0])
    rx.handle(rtp_packet(111, 0, 0, 14090, encode(_iq(180), "f32le")), lambda r, x: None)
    clock[0] += R.STALL_S + 1
    rx._stalls(clock[0])
    rx._stalls(clock[0] + 5)                                    # once per stall
    rx.handle(rtp_packet(111, 1, 180, 14090, encode(_iq(180), "f32le")), lambda r, x: None)
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("no packets" in m for m in msgs) == 1 and any("packets again" in m for m in msgs)


def test_two_bands_on_one_ssrc_warn(caplog) -> None:
    rx = RtpReceiver("127.0.0.1", port=0)
    rx.set_frequency(0, 14_090_200)
    rx.set_frequency(1, 14_089_900)
    assert any("both map to SSRC 14090" in r.getMessage() for r in caplog.records)


def test_iface_by_name() -> None:
    """rtp_iface may name an interface (lo) as well as give an address."""
    import struct
    rx = RtpReceiver("239.1.2.3", iface="lo")
    m = rx._membership("239.1.2.3")
    assert len(m) == 12 and struct.unpack("=4s4si", m)[2] == socket.if_nametoindex("lo")
    assert RtpReceiver("239.1.2.3", iface="127.0.0.1")._membership("239.1.2.3")[4:] == socket.inet_aton("127.0.0.1")
