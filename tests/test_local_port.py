"""telnet_local_port: the local port's own spot rule never reaches telnet_port (the RBN feed)."""

import types

import sparkgap
from spot_rule import RepeatSpotRule


class _Port:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def broadcast_spot(self, **spot) -> None:
        self.sent.append(spot['dx_call'])


def _skimmer(local_rule: object) -> types.SimpleNamespace:
    sk = types.SimpleNamespace(telnet=_Port(), telnet_local=_Port(),
                               tracker=types.SimpleNamespace(local_rule=local_rule))
    sk.broadcast = types.MethodType(sparkgap.SparkGap.broadcast, sk)
    return sk


def test_routing_with_a_local_rule() -> None:
    sk = _skimmer(local_rule=object())
    sk.broadcast('rbn', dx_call='W1AW')
    sk.broadcast('local', dx_call='K8TLJ')
    sk.broadcast('all', dx_call='N5CVT')
    sk.broadcast(dx_call='KA1XYZ')
    assert sk.telnet.sent == ['W1AW', 'N5CVT', 'KA1XYZ']
    assert sk.telnet_local.sent == ['K8TLJ', 'N5CVT', 'KA1XYZ']


def test_routing_without_a_local_rule_mirrors_the_rbn_port() -> None:
    sk = _skimmer(local_rule=None)
    sk.broadcast('rbn', dx_call='W1AW')
    assert sk.telnet.sent == sk.telnet_local.sent == ['W1AW']


def test_routing_without_a_local_port() -> None:
    sk = _skimmer(local_rule=None)
    sk.telnet_local = None
    sk.broadcast('rbn', dx_call='W1AW')
    sk.broadcast('all', dx_call='N5CVT')
    assert sk.telnet.sent == ['W1AW', 'N5CVT']


def test_a_spot_only_the_local_rule_passes_is_local_only() -> None:
    trk = sparkgap.SpotTracker(set(), set())
    trk.spot_rule = RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True)
    trk.local_rule = RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True, doubled=True)
    trk.clock = lambda: 100.0
    intent = sparkgap.SpotIntent(call='', freq_khz=7030.0, snr_db=12, wpm=20, is_runner=True,
                                 window_id=1, bin_id=1, window_text='CQ CQ DE K8TLJ K8TLJ K')
    spots = trk.process_intent(intent)
    assert [(s['call'], s['stream']) for s in spots] == [('K8TLJ', 'local')]
    intent2 = sparkgap.SpotIntent(call='', freq_khz=7030.0, snr_db=12, wpm=20, is_runner=True,
                                  window_id=2, bin_id=1, window_text='CQ CQ DE K8TLJ K8TLJ K')
    spots = trk.process_intent(intent2)
    assert [(s['call'], s['stream']) for s in spots] == [('K8TLJ', 'rbn')]   # local already spotted it
