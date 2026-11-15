"""pskr_feeder --telnet: SparkGap's CQ-tagged CW and RTTY telnet spots go to PSKReporter as mode CW / RTTY
(never a caller, never FT8 from telnet), each call once per mode and band in DEDUP_TIMEOUT."""

import struct
import types

import pskr_feeder as pf

CW = "DX de WF8Z-1-#: 14033.4  N5TOO          CW    21 dB  27 WPM  CQ      1337Z\a\a\r"
RTTY = "DX de WF8Z-1-#:  14085.5  DL1ABC         RTTY  18 dB  45 BPS  CQ      1338Z\r"


def _feeder():
    args = types.SimpleNamespace(call="WF8Z", grid="EM79SM", software="SparkGap", antenna="a", rig="r",
                                 host="127.0.0.1", port=4739, dry_run=True)
    return pf.PSKReporterFeeder(args)


def test_cq_cw_and_rtty_spots_queued() -> None:
    f = _feeder()
    a = f.on_telnet_line(CW, now=1000.0)
    b = f.on_telnet_line(RTTY, now=1000.0)
    assert a == {"call": "N5TOO", "grid": "", "freq_hz": 14033400, "snr": 21, "mode": "CW", "ts": 1000}
    assert b["mode"] == "RTTY" and b["call"] == "DL1ABC" and b["freq_hz"] == 14085500
    assert list(f.spot_queue) == [a, b] and f.totals["telnet_queued"] == 2


def test_callers_ft8_and_junk_not_reported() -> None:
    f = _feeder()
    assert f.on_telnet_line(CW.replace(" CQ ", " DE "), now=1.0) is None           # a caller's DE line
    assert f.on_telnet_line(CW.replace(" CW  ", " FT8 ").replace("27 WPM", "      "), now=1.0) is None
    assert f.on_telnet_line("To ALL de WF8Z: hello", now=1.0) is None
    assert not f.spot_queue


def test_dedup_per_call_mode_and_band() -> None:
    f = _feeder()
    assert f.on_telnet_line(CW, now=0.0)
    assert f.on_telnet_line(CW.replace("14033.4", "14034.0"), now=60.0) is None       # same call, band, mode
    assert f.on_telnet_line(CW.replace("14033.4", " 7025.0"), now=60.0)               # another band
    assert f.on_telnet_line(CW.replace(" CW  ", " RTTY").replace("WPM", "BPS"), now=60.0)   # another mode
    assert f.on_telnet_line(CW, now=pf.DEDUP_TIMEOUT + 1.0)                           # after the timeout
    assert f.totals["deduped"] == 1


def test_packet_carries_the_cw_record() -> None:
    """The IPFIX sender record: call, 5-byte frequency, SNR, mode, locator, source, time."""
    f = _feeder()
    spot = f.on_telnet_line(CW, now=1792605600.0)
    pkt = f.encoder.build_packet([spot], include_templates=True)
    assert struct.unpack_from(">H", pkt)[0] == 10 and struct.unpack_from(">H", pkt, 2)[0] == len(pkt)
    i = pkt.index(struct.pack(">H", pf.SENDER_TEMPLATE_ID), 16 + 4)                  # past the template set
    i = pkt.index(struct.pack(">H", pf.SENDER_TEMPLATE_ID), i + 2)                   # the data set
    body = pkt[i + 4:]
    n = body[0]
    assert body[1:1 + n] == b"N5TOO"
    j = 1 + n
    assert int.from_bytes(body[j:j + 5], "big") == 14033400 and struct.unpack_from(">b", body, j + 5)[0] == 21
    m = body[j + 6]
    assert body[j + 7:j + 7 + m] == b"CW"
    k = j + 7 + m
    assert body[k] == 0 and body[k + 1] == 1 and struct.unpack_from(">I", body, k + 2)[0] == 1792605600
