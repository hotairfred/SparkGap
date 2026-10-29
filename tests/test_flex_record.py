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

    def __init__(self, ip, **kw):
        self.kw = kw
        FakeRx.made.append(self)

    def start(self):
        pass

    def receive(self, cb, duration=None):
        cb(0, [0.0, 0.0] * 4)

    def stop(self):
        pass


@pytest.fixture
def record(monkeypatch, tmp_path):
    FakeRx.made = []
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
