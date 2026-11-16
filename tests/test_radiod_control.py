"""radiod_control: rtp_create_channels asks radiod for the bands' IQ channels (ka9q-radio status.h commands),
keeps them alive, moves them on a band change and ends them on stop."""

import socket
import struct

import pytest

import radiod_control as rc
from rtp_receiver import RtpReceiver


def tlvs(pkt):
    """{tag: value bytes} of a command, checking its framing: CMD first, EOL last."""
    assert pkt[0] == rc.CMD and pkt[-1] == rc.EOL
    out, i = {}, 1
    while pkt[i] != rc.EOL:
        tag, n = pkt[i], pkt[i + 1]
        assert n < 128
        out[tag] = pkt[i + 2:i + 2 + n]
        i += 2 + n
    assert i == len(pkt) - 1
    return out


def num(b):
    return int.from_bytes(b, "big")


def test_integers_drop_leading_zero_bytes_as_ka9q() -> None:
    assert rc._int(rc.OUTPUT_SSRC, 14077) == bytes([18, 2, 0x36, 0xFD])
    assert rc._int(rc.AGC_ENABLE, 0) == bytes([62, 0])           # zero is zero length
    assert rc._string(rc.PRESET, "iq") == bytes([85, 2]) + b"iq"


def test_channel_command() -> None:
    t = tlvs(rc.channel_command(14077, 14_076_800, samprate=192000, encoding="f32le", tag=7))
    assert num(t[rc.COMMAND_TAG]) == 7 and num(t[rc.OUTPUT_SSRC]) == 14077
    assert t[rc.PRESET] == b"iq"
    assert struct.unpack(">d", t[rc.RADIO_FREQUENCY].rjust(8, b"\0"))[0] == 14_076_800.0
    assert num(t[rc.OUTPUT_SAMPRATE]) == 192000
    assert struct.unpack(">f", t[rc.LOW_EDGE].rjust(4, b"\0"))[0] == -95000.0
    assert struct.unpack(">f", t[rc.HIGH_EDGE].rjust(4, b"\0"))[0] == 95000.0
    assert num(t[rc.OUTPUT_ENCODING]) == 4                          # ka9q rtp.h F32LE
    assert t[rc.AGC_ENABLE] == b"" and t[rc.GAIN] == b""            # AGC off, 0 dB (zero: zero length)
    assert num(t[rc.LIFETIME]) == round(rc.LIFETIME_S / rc.BLOCK_S)
    assert num(tlvs(rc.channel_command(1, 1e6, encoding="s16be"))[rc.OUTPUT_ENCODING]) == 2
    with pytest.raises(ValueError):
        rc.channel_command(1, 1e6, encoding="opus")


def test_expire_command_only_names_ssrc_and_lifetime() -> None:
    t = tlvs(rc.expire_command(14077, tag=3))
    assert set(t) == {rc.COMMAND_TAG, rc.OUTPUT_SSRC, rc.LIFETIME} and num(t[rc.LIFETIME]) == 1


@pytest.fixture()
def listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(2)
    yield s
    s.close()


def recv_all(s, n):
    return [tlvs(s.recv(2048)) for _ in range(n)]


def test_keeper_creates_moves_and_ends_channels(listener) -> None:
    port = listener.getsockname()[1]
    k = rc.ChannelKeeper("127.0.0.1", {14077: 14_076_800, 7077: 7_076_800}, port=port, refresh_s=3600)
    k.start()
    got = recv_all(listener, 2)
    assert sorted(num(t[rc.OUTPUT_SSRC]) for t in got) == [7077, 14077]
    k.set_channels({14077: 14_076_800, 21077: 21_076_800})          # 40 m out, 15 m in
    got = recv_all(listener, 3)
    made = sorted(num(t[rc.OUTPUT_SSRC]) for t in got if rc.PRESET in t)
    ended = [num(t[rc.OUTPUT_SSRC]) for t in got if rc.PRESET not in t]
    assert made == [14077, 21077] and ended == [7077]
    k.stop()
    got = recv_all(listener, 2)
    assert sorted(num(t[rc.OUTPUT_SSRC]) for t in got) == [14077, 21077]
    assert all(num(t[rc.LIFETIME]) == 1 for t in got)
    k.stop()                                                         # twice: nothing more sent
    listener.settimeout(0.2)
    with pytest.raises(OSError):
        listener.recv(2048)


def test_keeper_refreshes(listener) -> None:
    k = rc.ChannelKeeper("127.0.0.1", {14077: 14_076_800}, port=listener.getsockname()[1], refresh_s=0.05)
    k.start()
    recv_all(listener, 3)                                            # the first command and two refreshes
    k.stop()


def test_receiver_asks_only_with_a_status_group(monkeypatch) -> None:
    started = []

    class Keeper:
        def __init__(self, group, channels, **kw):
            started.append((group, dict(channels), kw["samprate"], kw["encoding"]))

        def start(self):
            pass

        def stop(self):
            pass

        def set_channels(self, ch):
            started.append(("moved", dict(ch)))

    monkeypatch.setattr(rc, "ChannelKeeper", Keeper)
    monkeypatch.setattr(RtpReceiver, "_open", lambda self: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
    plain = RtpReceiver("sparkgap-iq.local")
    plain.set_frequency(0, 14_076_800)
    plain.start()
    plain.close()
    assert started == []                                             # no status group: radiod's config has them
    rx = RtpReceiver("sparkgap-iq.local", encoding="s16be", status="sparkgap-status.local")
    rx.set_frequency(0, 14_076_800)
    rx.set_frequency(1, 7_076_800)
    rx.start()
    assert started == [("sparkgap-status.local", {14077: 14_076_800, 7077: 7_076_800}, 192000, "s16be")]
    rx.retune(21_076_800)
    assert started[-1] == ("moved", {21077: 21_076_800, 7077: 7_076_800})
    rx.close()
