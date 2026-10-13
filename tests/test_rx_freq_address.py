"""HPSDR Protocol 1 receiver frequency registers: RX8 is C0 0x24, not 0x12."""
import re

import hpsdr_receiver as H


def test_rx_freq_c0_map():
    assert [H.rx_freq_c0(i) for i in range(8)] == [0x04, 0x06, 0x08, 0x0A, 0x0C, 0x0E, 0x10, 0x24]


def test_build_freq_packet_rx8():
    assert H.build_freq_packet(7, 28090000) == bytes([0x24]) + (28090000).to_bytes(4, 'big')


def test_c_receiver_uses_same_map():
    """hpsdr_fast.c's rx_freq_c0() must match (the C path runs all multi-band
    skimmers)."""
    src = open('hpsdr_fast.c').read()
    body = re.search(r'static uint8_t rx_freq_c0\(int rx_index\) \{(.*?)\n\}', src, re.S).group(1)
    assert 'rx_index == 7 ? 0x24' in body and '(rx_index + 2) * 2' in body
    assert 'freq[0] = rx_freq_c0(i);' in src
