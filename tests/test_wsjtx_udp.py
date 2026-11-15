"""wsjtx_udp.py: SparkGap's FT8/FT4/WSPR decodes as WSJT-X UDP messages (NetworkMessage.hpp, schema 2), and
the dial, offset, slot and DT that sparkgap.py adds to each skimmer/ft8/raw payload for it."""

import json
import struct
import types

import numpy as np
import pytest

import sparkgap
import wsjtx_udp as w


class Reader:
    """Just enough of a QDataStream reader to check what wsjtx_udp writes."""

    def __init__(self, b):
        self.b, self.i = b, 0

    def take(self, fmt):
        v = struct.unpack_from('>' + fmt, self.b, self.i)
        self.i += struct.calcsize('>' + fmt)
        return v[0]

    def utf8(self):
        n = self.take('I')
        if n == 0xFFFFFFFF:
            return None
        s = self.b[self.i:self.i + n].decode()
        self.i += n
        return s

    def head(self):
        assert self.take('I') == w.MAGIC and self.take('I') == w.SCHEMA
        return self.take('I'), self.utf8()

    def done(self):
        return self.i == len(self.b)


def test_decode_message() -> None:
    r = Reader(w.decode('SG 20m FT8', 1792605645.0, -5, 0.3, 1803, 'FT8', 'CQ N4DWD EM86'))
    assert r.head() == (w.DECODE, 'SG 20m FT8')
    assert r.take('?') is True
    assert r.take('I') == (1792605645 % 86400) * 1000           # ms since UTC midnight
    assert (r.take('i'), r.take('d'), r.take('I')) == (-5, 0.3, 1803)
    assert (r.utf8(), r.utf8()) == ('~', 'CQ N4DWD EM86')
    assert (r.take('?'), r.take('?')) == (False, False) and r.done()


def test_status_message() -> None:
    r = Reader(w.status('SparkGap 40m FT4', 7047500, 'FT4', 'WF8Z', 'EM79'))
    assert r.head() == (w.STATUS, 'SparkGap 40m FT4')
    assert (r.take('Q'), r.utf8(), r.utf8(), r.utf8(), r.utf8()) == (7047500, 'FT4', '', '', 'FT4')
    assert (r.take('?'), r.take('?'), r.take('?')) == (False, False, True)
    r.take('I'), r.take('I')
    assert (r.utf8(), r.utf8(), r.utf8()) == ('WF8Z', 'EM79', '')
    r.take('?'), r.utf8(), r.take('?'), r.take('B'), r.take('I')
    assert r.take('I') == 7 and r.utf8() == 'SparkGap' and r.utf8() == '' and r.done()


def test_wspr_message() -> None:
    call, grid, dbm = w.wspr_fields('K1ABC FN42 37')
    r = Reader(w.wspr_decode('SparkGap 20m WSPR', 1792605600, -22, 0.4, 14097063, -1, call, grid, dbm))
    assert r.head() == (w.WSPR_DECODE, 'SparkGap 20m WSPR')
    r.take('?'), r.take('I')
    assert (r.take('i'), r.take('d'), r.take('Q'), r.take('i')) == (-22, 0.4, 14097063, -1)
    assert (r.utf8(), r.utf8(), r.take('i'), r.take('?')) == ('K1ABC', 'FN42', 37, False) and r.done()
    assert w.wspr_fields('PJ4/K1ABC 37') == ('PJ4/K1ABC', '', 37)
    assert w.wspr_fields('<K1ABC> FN42AB 37') == ('<K1ABC>', 'FN42AB', 37)


def _sender():
    s = w.Sender([], w.DEFAULT_NAME, 'WF8Z', 'EM79', 'test')
    s.pkts = []
    s.send = lambda pkt, what='': s.pkts.append(Reader(pkt).head())
    return s


def test_one_instance_per_band_and_mode_announced_once() -> None:
    s = _sender()
    p = {'freq_hz': 14075803, 'snr': -5, 'mode': 'FT8', 'msg': 'CQ N4DWD EM86', 'ts': 1792605660,
         'dial_hz': 14074000, 'df_hz': 1803, 'slot_ts': 1792605645.0, 'dt': 0.3}
    s.on_ftx(p)
    s.on_ftx(dict(p, msg='CQ K4DB EM84'))
    s.on_ftx(dict(p, mode='FT4', dial_hz=14080000, freq_hz=14081500, df_hz=1500))
    assert s.pkts == [(w.HEARTBEAT, 'SG 20m FT8'), (w.STATUS, 'SG 20m FT8'),
                      (w.DECODE, 'SG 20m FT8'), (w.DECODE, 'SG 20m FT8'),
                      (w.HEARTBEAT, 'SG 20m FT4'), (w.STATUS, 'SG 20m FT4'),
                      (w.DECODE, 'SG 20m FT4')]
    s.pkts.clear()
    s.close_all()
    assert sorted(s.pkts) == [(w.CLOSE, 'SG 20m FT4'), (w.CLOSE, 'SG 20m FT8')]


def test_payload_without_dial_uses_the_standard_dial() -> None:
    """Builds before this publish no dial_hz: the standard dial below the frequency stands in."""
    assert w.ftx_spot({'freq_hz': 7048700, 'mode': 'FT4', 'snr': 3, 'msg': 'x', 'ts': 1792605660})[:2] == (7047500, 1200)
    assert w.ftx_spot({'freq_hz': 14075803, 'mode': 'FT8', 'snr': 3, 'msg': 'x', 'ts': 1792605660})[:2] == (14074000, 1803)


@pytest.mark.parametrize("tx_t", [1792605645.5, 1792605645.9, 1792605652.0, 1792605644.0])
def test_slot_time_matches_ft8_slot_dt(tx_t) -> None:
    slot, dt = sparkgap.ftx_slot_time(tx_t, 15.0)
    assert slot % 15 == 0 and dt == pytest.approx(sparkgap.ft8_slot_dt(tx_t, 0.0), abs=0.01)
    assert sparkgap.ftx_slot_time(1792605652.6, 7.5) == (1792605652.5, -0.4)


def test_cycle_publishes_dial_offset_slot_and_dt(monkeypatch) -> None:
    sent = []

    def fake_run(args, **kw):
        return types.SimpleNamespace(stdout="000000 +05.0 +0.80 1803 ~  CQ N4DWD EM86\n")

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    pub = types.SimpleNamespace(publish=lambda topic, payload, qos=0: sent.append((topic, json.loads(payload))))
    monkeypatch.setattr(sparkgap, "_get_mqtt_publisher", lambda: pub)
    sk = types.SimpleNamespace(cfg={"enable_ft8": True, "enable_rtty": False}, spot_count=0, managers=[],
                               _corrected_freq_khz=lambda f: f, broadcast=lambda **s: None)
    z = np.zeros(60 * 192000, dtype=np.float32)
    sparkgap._ft8_cycle([("20m", 0, 14090.0, 14074, z, z, len(z), 1792605660.0)], sk)
    topic, d = sent[0]
    assert topic == 'skimmer/ft8/raw' and d['freq_hz'] == 14075803
    assert (d['dial_hz'], d['df_hz']) == (14074000, 1803)
    assert d['slot_ts'] % 15 == 0 and -1.5 < d['dt'] < 2.5
    assert w.ftx_spot(d)[:2] == (14074000, 1803)


def test_instance_names() -> None:
    assert w.instance_name(w.DEFAULT_NAME, 14074000, 'FT8') == 'SG 20m FT8'
    assert w.instance_name('{band}{mode}', 7047500, 'FT4') == '40mFT4'
    assert w.instance_name('{call} {dial}', 7047500, 'FT4', 'WF8Z') == 'WF8Z 7047.5'
    w.check_name('SparkGap {band} {mode}')
    for bad in ('SG', 'SG {band}', 'SG {mode}', 'SG {band} {mode} {nope}', 'SG {band} {mode} {'):
        with pytest.raises(ValueError):
            w.check_name(bad)
