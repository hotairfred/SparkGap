"""flex_record.py without a radio: --rxant, --loopa and --loopb reach every receiver, and left out they
leave the radio's band setting alone (None)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'voice'))
import flex_record  # noqa: E402


class FakeRx:
    made = []
    refuse = {}       # DAX-IQ channel -> the RuntimeError its start() raises

    def __init__(self, ip, **kw):
        self.kw = kw
        self.stopped = False
        FakeRx.made.append(self)

    def start(self):
        err = FakeRx.refuse.get(self.kw['daxiq_channel'])
        if err:
            raise RuntimeError(err)

    def receive(self, cb, duration=None):
        cb(0, [0.0, 0.0] * 4)

    def stop(self):
        self.stopped = True


@pytest.fixture
def record(monkeypatch, tmp_path):
    FakeRx.made, FakeRx.refuse = [], {}
    monkeypatch.setattr(flex_record, 'FlexIQReceiver', FakeRx)

    def run(*extra):
        monkeypatch.setattr(sys, 'argv', ['flex_record.py', '192.0.2.1', str(tmp_path), '0.1',
                                          'auto:14246000', 'auto:7200000', *extra])
        flex_record.main()
        return [r.kw for r in FakeRx.made]
    return run


def test_the_receive_path_reaches_every_receiver(record, tmp_path) -> None:
    kws = record('--rxant', 'ANT1', '--loopb', '1')
    assert [(k['rxant'], k['loopa'], k['loopb']) for k in kws] == [('ANT1', None, 1), ('ANT1', None, 1)]
    assert [k['daxiq_channel'] for k in kws] == [1, 2]
    sidecars = sorted(p for p in os.listdir(tmp_path) if p.endswith('.json'))
    assert [json.load(open(tmp_path / p))['centre_hz'] for p in sidecars] == [14246000, 7200000]


def test_left_out_the_radio_keeps_its_band_setting(record) -> None:
    kws = record()
    assert all(k['rxant'] is None and k['loopa'] is None and k['loopb'] is None for k in kws)


def test_a_busy_channel_moves_on_to_the_next(record) -> None:
    FakeRx.refuse = {1: "[Flex] DAX-IQ 1 is in use by SmartSDR on pan 0x40000000; ..."}
    kws = record()
    assert [k['daxiq_channel'] for k in kws] == [1, 2, 1, 3]   # each band: 1 is busy, so 2, then 3


def test_a_refused_receive_path_stops_with_the_radio_s_reason(record) -> None:
    FakeRx.refuse = {2: "[Flex] The radio refused rxant=ANT3 on slice 1"}
    with pytest.raises(SystemExit, match="refused rxant=ANT3"):
        record('--rxant', 'ANT3')
    assert [k['daxiq_channel'] for k in (r.kw for r in FakeRx.made)] == [1, 2]   # no channel 3 or 4 tried
    assert FakeRx.made[0].stopped                                                 # the first band let go


def test_help_prints_usage_once(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, 'argv', ['flex_record.py', '-h'])
    with pytest.raises(SystemExit):
        flex_record.main()
    assert capsys.readouterr().out.lower().count('usage:') == 1
