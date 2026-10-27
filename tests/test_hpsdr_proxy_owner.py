"""hpsdr_proxy.py --owner: only the owner's C&C reaches the radio; a listener's stays at the proxy, and one
asking for another frequency is logged."""
import socket

import hpsdr_proxy as hp


def cc_packet(frames):
    """A C&C packet (EF FE 01 02, sequence, two 512-byte frames) with (C0, value) in each frame."""
    pkt = bytearray(b'\xef\xfe\x01\x02' + bytes(4) + bytes(1024))
    for k, (c0, value) in enumerate(frames):
        off = 8 + 512 * k
        pkt[off:off + 3] = b'\x7f\x7f\x7f'
        pkt[off + 3] = c0
        pkt[off + 4:off + 8] = value.to_bytes(4, 'big')
    return bytes(pkt)


def test_rx_freqs_reads_rx1_to_rx8() -> None:
    assert hp.rx_freqs(cc_packet([(0x04, 7_090_000), (0x24, 28_090_000)])) == {0: 7_090_000, 7: 28_090_000}
    assert hp.rx_freqs(cc_packet([(0x05, 3_590_000), (0x10, 24_902_000)])) == {0: 3_590_000, 6: 24_902_000}  # MOX bit
    assert hp.rx_freqs(cc_packet([(0x00, 123), (0x12, 456)])) == {}          # config and drive registers


def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 0))
    s.settimeout(0.3)
    return s


def got(sock):
    try:
        return sock.recvfrom(2048)[0]
    except socket.timeout:
        return None


def test_only_the_owner_tunes_the_radio(capsys) -> None:
    radio, owner, listener = udp(), udp(), udp()
    o_addr, l_addr = owner.getsockname(), listener.getsockname()
    p = hp.HPSDRProxy('127.0.0.1', radio.getsockname()[1], 0, owner='127.0.0.1:%d' % o_addr[1])
    try:
        mine = cc_packet([(0x04, 7_090_000), (0x06, 14_090_000)])
        p._handle_consumer(mine, o_addr)
        assert got(radio) == mine                                    # the owner's goes to the radio
        same = cc_packet([(0x04, 7_090_000), (0x06, 14_090_000)])
        p._handle_consumer(same, l_addr)
        assert got(radio) is None                                    # a listener's never does
        assert '!' not in capsys.readouterr().err                   # and matching bands raise nothing
        other = cc_packet([(0x04, 7_040_000), (0x06, 14_090_000)])
        p._handle_consumer(other, l_addr)
        p._handle_consumer(other, l_addr)
        assert got(radio) is None
        err = capsys.readouterr().err
        assert err.count('wants RX1 on 7040.0 kHz') == 1 and 'owner has it on 7090.0 kHz' in err
    finally:
        p.close()
        for s in (radio, owner, listener):
            s.close()


def test_without_an_owner_every_consumer_tunes() -> None:
    radio, a = udp(), udp()
    p = hp.HPSDRProxy('127.0.0.1', radio.getsockname()[1], 0)
    try:
        pkt = cc_packet([(0x04, 7_090_000), (0x06, 14_090_000)])
        p._handle_consumer(pkt, a.getsockname())
        assert got(radio) == pkt
    finally:
        p.close()
        radio.close()
        a.close()
