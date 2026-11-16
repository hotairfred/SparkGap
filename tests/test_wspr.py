"""enable_wspr: each band's WSPR dial is cut from the minute snapshots, two minutes joined into the 2-minute
cycle and decoded by wsprd; each decode is published on MQTT skimmer/wspr/raw. wspr_feeder.py posts them to
WSPRnet as WSJT-X does."""

import json
import types

import numpy as np
import pytest

import sparkgap
import wspr_feeder as wf

T_EVEN = 1792551360.0                  # an even UTC minute
WSPRD_OUT = (" 1208  16  5.8  14.097064  0  KR4CQD EL96 23 \n"
             " 1208 -10  0.1  14.097083  0  ON6FDP/P 37 \n"
             " 1208 -29  0.1  14.097155  0  <WA1KPD/C> FN54FA 23 \n"
             "<DecodeFinished>\n")


def test_wspr_dials_in_the_span() -> None:
    assert sparkgap.wspr_dial_khz(14090) == 14095.6
    assert sparkgap.wspr_dial_khz(1886.8) == 1836.6
    assert sparkgap.wspr_dial_khz(28090) == 28124.6
    assert sparkgap.wspr_dial_khz(14040, fs=48000) is None


def test_even_minute_held_odd_joins() -> None:
    sk = types.SimpleNamespace()
    n = 60 * 12000
    a, b = np.ones(n, np.complex64), 2 * np.ones(n, np.complex64)
    assert sparkgap._wspr_keep(sk, "20m", 14095.6, T_EVEN, a) is None
    minute, dial, x = sparkgap._wspr_keep(sk, "20m", 14095.6, T_EVEN + 60, b)
    assert minute == T_EVEN / 60 and dial == 14095.6 and len(x) == 2 * n
    assert x[0] == 1 and x[n - 1] == 1 and x[n] == 2 and x[-1] == 2
    assert sparkgap._wspr_keep(sk, "20m", 14095.6, T_EVEN + 180, b) is None    # odd, no even before it
    sparkgap._wspr_keep(sk, "20m", 14095.6, T_EVEN, a)
    assert sparkgap._wspr_keep(sk, "20m", 14095.6, T_EVEN + 180, b) is None    # a minute missing


def test_short_snapshots_join_on_time() -> None:
    """Review: a live minute can start up to 1 s late; each is placed on its minute, so the join has no gap."""
    sk = types.SimpleNamespace()
    n, fs = 60 * 12000, 12000
    t = np.arange(2 * n)                                   # a sample's index in the 2 minutes = its time
    first = t[int(0.8 * fs):n].astype(np.complex64)        # started 0.8 s late
    second = t[n + int(0.5 * fs):2 * n - fs // 4].astype(np.complex64)   # 0.5 s late, ended 0.25 s early
    sparkgap._wspr_keep(sk, "20m", 14095.6, T_EVEN + 0.8, first)
    _, _, x = sparkgap._wspr_keep(sk, "20m", 14095.6, T_EVEN + 60.5, second)
    assert len(x) == 2 * n
    have = x != 0
    assert np.array_equal(x[have].real, t[have])           # every sample at its own time
    assert not have[:int(0.8 * fs)].any() and not have[n:n + int(0.5 * fs)].any()


def _stub(monkeypatch):
    runs, pubs = [], []

    def fake_run(args, **kw):
        runs.append(args)
        return types.SimpleNamespace(stdout=WSPRD_OUT)

    class Pub:
        def publish(self, topic, payload, qos=0):
            pubs.append((topic, json.loads(payload)))

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sparkgap, "_get_mqtt_publisher", lambda: Pub())
    return runs, pubs


def test_decode_publishes_each_spot(monkeypatch, tmp_path) -> None:
    runs, pubs = _stub(monkeypatch)
    sk = types.SimpleNamespace()
    x = np.zeros(120 * 12000, np.complex64)
    n = sparkgap._wspr_decode([("20m", int(T_EVEN // 60), 14095.6, x)], sk, ("/opt/wsprd", True, str(tmp_path)))
    assert n == 3 and sk._wspr_running is False
    assert runs[0][:3] == ["/opt/wsprd", "-f", "14.095600"] and "-d" in runs[0]
    assert not list(tmp_path.rglob("*.wav"))                  # the cycle's WAV is removed
    assert {p[0] for p in pubs} == {"skimmer/wspr/raw"}
    assert pubs[0][1] == {"date": "261021", "time": "1208", "snr": 16, "dt": 5.8, "freq_hz": 14097064,
                          "drift": 0, "msg": "KR4CQD EL96 23", "dial_hz": 14095600, "ts": int(T_EVEN)}
    assert [p[1]["msg"] for p in pubs] == ["KR4CQD EL96 23", "ON6FDP/P 37", "<WA1KPD/C> FN54FA 23"]


def test_cycle_decodes_once_per_two_minutes(monkeypatch) -> None:
    runs, pubs = _stub(monkeypatch)
    sk = types.SimpleNamespace(cfg={"enable_ft8": False, "enable_rtty": False, "enable_wspr": True,
                                    "wsprd_path": "/opt/wsprd"},
                               spot_count=0, managers=[], _corrected_freq_khz=lambda f: f,
                               broadcast=lambda **s: None, wspr_sync=True)
    z = np.zeros(60 * 192000, dtype=np.float32)
    for k in range(4):                                         # even, odd, even, odd
        sparkgap._ft8_cycle([("20m", 0, 14090.0, 14074, z, z, len(z), T_EVEN + 60 * k)], sk)
    assert len(runs) == 2 and len(pubs) == 6


def test_off_by_default(monkeypatch) -> None:
    assert sparkgap._wspr_decoder({}) is None
    assert sparkgap._gui_settings({}, 192000)[0]["enable_wspr"] is False


def test_feeder_message_shapes() -> None:
    assert wf.parse_message("KR4CQD EL96 23") == ("KR4CQD", "EL96", 23)
    assert wf.parse_message("ON6FDP/P 37") == ("ON6FDP/P", "", 37)
    assert wf.parse_message("<WA1KPD/C> FN54FA 23") == ("WA1KPD/C", "FN54FA", 23)
    assert wf.parse_message("<WA1KPD/C> 23") is None          # WSJT-X posts none of these
    assert wf.parse_message("1D2VNA IH53 13") == ("1D2VNA", "IH53", 13)   # balloon telemetry: as WSJT-X


def _args(**kw):
    return types.SimpleNamespace(**{"call": "WF8Z", "grid": "EM79SM", "software": "SparkGap", "url": "http://x/",
                                    "dry_run": False, **kw})


SPOT = {"date": "261114", "time": "1208", "snr": -12, "dt": -0.14, "freq_hz": 14097154, "drift": 0,
        "msg": "AC1JS FN54 23", "dial_hz": 14095600, "ts": 0}


def test_feeder_form_as_wsjtx() -> None:
    q = dict(p.split("=", 1) for p in wf.spot_query(SPOT, "WF8Z", "EM79SM", "SparkGap").split("&"))
    assert q == {"function": "wspr", "rcall": "WF8Z", "rgrid": "EM79SM", "rqrg": "14.095600", "date": "261114",
                 "time": "1208", "sig": "-12", "dt": "-0.1", "drift": "0", "tqrg": "14.097154", "tcall": "AC1JS",
                 "tgrid": "FN54", "dbm": "23", "version": "SparkGap", "mode": "2"}


def test_feeder_dedup_post_and_retry(monkeypatch) -> None:
    monkeypatch.setattr(wf, "RETRY_S", 0.0)
    replies = iter([OSError("down"), "1 spot(s) added"])

    def post(q):
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r

    f = wf.WsprFeeder(_args(), post=post)
    assert f.on_spot(SPOT) is not None and f.on_spot(dict(SPOT)) is None     # same cycle, call and dial: once
    assert f.on_spot({**SPOT, "msg": "<X> 23"}) is None
    assert f.send_one(f.q.get()) is True
    assert f.totals == {"received": 3, "not_wsprnet_shape": 1, "deduped": 1, "posted": 1, "failed": 0}
    refused = wf.WsprFeeder(_args(), post=lambda q: "Error: bad call")
    assert refused.send_one("x") is False and refused.totals["failed"] == 1


def test_feeder_dry_run_posts_nothing() -> None:
    f = wf.WsprFeeder(_args(dry_run=True), post=lambda q: pytest.fail("posted in a dry run"))
    assert f.send_one(f.on_spot(SPOT)) is True


def test_wsprd_logs_removed_hash_table_kept(monkeypatch, tmp_path) -> None:
    """Review: wsprd -a appends ALL_WSPR.TXT each cycle; it goes after the decode, the hash table stays."""
    d = tmp_path / "20m"

    def fake_run(args, **kw):
        d.mkdir(exist_ok=True)
        for f in ("ALL_WSPR.TXT", "wspr_spots.txt", "hashtable.txt"):
            (d / f).write_text("x")
        return types.SimpleNamespace(stdout=WSPRD_OUT)

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sparkgap, "_get_mqtt_publisher", lambda: None)
    sparkgap._wspr_decode([("20m", int(T_EVEN // 60), 14095.6, np.zeros(10, np.complex64))],
                          types.SimpleNamespace(), ("wsprd", False, str(tmp_path)))
    assert sorted(p.name for p in d.iterdir()) == ["hashtable.txt"]


def test_wspr_alone_takes_the_minute() -> None:
    """Review: WSPR (or FST4W) on with FT8 and RTTY off still cuts each band's minute."""
    assert sparkgap._minute_modes({"enable_ft8": False, "enable_rtty": False, "enable_wspr": True})
    assert sparkgap._minute_modes({"enable_ft8": False, "enable_rtty": False, "enable_fst4w": True})
    assert not sparkgap._minute_modes({"enable_ft8": False, "enable_rtty": False})
    assert sparkgap._minute_modes({})                       # FT8 and RTTY on unless set off, as before


def test_feeder_subscribes_on_every_connect(monkeypatch) -> None:
    """Review: a broker restart drops the subscription; it is made again on each (re)connect."""
    import sys

    subs = []

    class Client:
        def __init__(self, *a, **k):
            self.on_connect = None

        def username_pw_set(self, *a):
            pass

        def connect(self, *a, **k):
            pass

        def loop_start(self):
            self.on_connect(self, None, None, 0)
            self.on_connect(self, None, None, 0)           # reconnected

        def subscribe(self, topic, qos=0):
            subs.append(topic)

        def loop_stop(self):
            pass

        def disconnect(self):
            pass

    mod = types.SimpleNamespace(Client=Client, CallbackAPIVersion=types.SimpleNamespace(VERSION2=2))
    monkeypatch.setitem(sys.modules, "paho", types.SimpleNamespace(mqtt=types.SimpleNamespace(client=mod)))
    monkeypatch.setitem(sys.modules, "paho.mqtt", types.SimpleNamespace(client=mod))
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", mod)
    monkeypatch.setattr(wf.threading, "Thread", lambda **k: types.SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(wf.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt))
    f = wf.WsprFeeder(_args(topic="skimmer/wspr/raw", mqtt_user="", mqtt_pass="", mqtt_host="h", mqtt_port=1,
                            url="http://x/"))
    try:
        f.run()
    except KeyboardInterrupt:
        pass
    assert subs == ["skimmer/wspr/raw", "skimmer/wspr/raw"]
