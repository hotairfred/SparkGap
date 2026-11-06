"""One RBN node per radio (cdub89/SparkGap#33): identity and rbn_upload in the radio file, one
feeder fed by all the radio's receivers, a call two receivers copy posted once per interval."""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import rbn2_feeder as f

RADIO = {"type": "flex", "nickname": "Lake", "address": "192.0.2.7", "callsign": "N0CALL-2",
         "grid": "EM12ub", "op_name": "Op", "rbn_upload": "dry"}


def _receiver(name: str, port: int, validation: str = "Normal",
              respot: float = 120.0) -> f.Receiver:
    return f.Receiver(name, port, respot, validation)


def test_the_node_takes_its_identity_from_the_radio_file() -> None:
    node = f.node_settings(RADIO, [_receiver("Lake-1", 7301), _receiver("Lake-2", 7302)])
    assert (node.call, node.grid, node.name, node.mode, node.port) == (
        "N0CALL-2", "EM12ub", "Op", "dry", 7301)


def test_the_node_reports_the_least_validation_of_its_receivers() -> None:
    rx = [_receiver("Lake-1", 7301, "Aggressive"), _receiver("Lake-2", 7302, "Permissive")]
    assert f.node_settings(RADIO, rx).validation == "Permissive"


@pytest.mark.parametrize("change, why", [
    ({"rbn_upload": None}, "off"),
    ({"callsign": ""}, "callsign and grid"),
    ({"grid": None}, "callsign and grid"),
])
def test_a_node_that_cannot_feed_says_why(change: dict[str, Any], why: str) -> None:
    with pytest.raises(ValueError, match=why):
        f.node_settings({**RADIO, **change}, [_receiver("Lake-1", 7301)])


def test_a_node_needs_a_receiver() -> None:
    with pytest.raises(ValueError, match="no receiver"):
        f.node_settings(RADIO, [])


def test_a_receiver_s_settings_come_from_its_config(tmp_path: Path) -> None:
    (tmp_path / "Lake-3.local.json").write_text(json.dumps(
        {"telnet_port": 7303, "respot_interval": 300, "spot_validation": "normal"}))
    assert f.receiver_settings(tmp_path, "Lake-3") == f.Receiver("Lake-3", 7303, 300.0, "Normal")
    with pytest.raises(ValueError, match="receiver Lake-4"):
        f.receiver_settings(tmp_path, "Lake-4")


def test_a_call_two_receivers_copy_is_posted_once_per_interval() -> None:
    r = f.Respot(120.0)
    assert r.allow("K1ABC", 7030.1, 0.0)
    assert not r.allow("K1ABC", 7030.3, 2.0)  # the second receiver's copy
    assert r.allow("K1ABC", 7032.0, 3.0)  # moved more than 1 kHz: a new spot
    assert r.allow("W1AW", 7030.1, 3.0)
    assert not r.allow("K1ABC", 7030.1, 100.0)
    assert r.allow("K1ABC", 7030.1, 116.0)  # a receiver's own re-spot, a little early


def test_the_upload_drops_a_call_another_receiver_just_sent() -> None:
    spot = {"freq": 7027.6, "call": "JA0FVU", "cq_flag": "CQ", "snr": 25, "wpm": 30, "mode": "CW"}
    spots: queue.Queue[dict[str, Any]] = queue.Queue()
    for s in (spot, {**spot, "freq": 7027.7}, {**spot, "call": "K1ABC"}):
        spots.put(s)
    sess = f.Session(url="http://rbn.invalid/", call="N0CALL-2", name="", grid="EM12ub", qth="",
                     validation="Normal", port=7301, bands=[], auth=None, version=f.SKIM_VERSION,
                     fingerprint="f" * 32, dry_run=True)
    sess.registered.set()
    sess.upload_s = 0.01
    shutdown = threading.Event()
    t = threading.Thread(target=f.upload_loop,
                         args=(sess, spots, set(), shutdown, lambda: 50.0, f.Respot()))
    t.start()
    end = time.monotonic() + 2
    while sess.counts["sent"] < 2 and time.monotonic() < end:
        time.sleep(0.01)
    shutdown.set()
    t.join()
    assert (sess.counts["sent"], sess.counts.get("respot")) == (2, 1)


def test_the_readers_follow_the_receivers_the_server_names() -> None:
    reading: dict[int, threading.Event] = {}

    def read(_host: str, port: int, _call: str, _spots: Any, stop: threading.Event) -> None:
        reading[port] = stop
        stop.wait()

    shutdown, failed = threading.Event(), threading.Event()
    respot = f.Respot()
    readers = f.Readers("N0CALL-2", queue.Queue(), shutdown, failed, respot, read)
    readers.set([_receiver("Lake-1", 7301), _receiver("Lake-2", 7302, respot=300.0)])
    end = time.monotonic() + 2
    while len(reading) < 2 and time.monotonic() < end:
        time.sleep(0.01)
    assert sorted(readers.ports()) == [7301, 7302] and respot.interval_s == 300.0
    readers.set([_receiver("Lake-2", 7302, respot=300.0)])
    assert reading[7301].is_set() and not reading[7302].is_set()
    assert sorted(readers.receivers) == ["Lake-2"]
    readers.stop_all()
    assert reading[7302].is_set() and not failed.is_set()


def test_the_node_s_bands_are_every_receiver_s(monkeypatch: pytest.MonkeyPatch) -> None:
    sett = {7301: [(7000.0, 7096.0)], 7302: [(14000.0, 14096.0), (7000.0, 7096.0)]}
    monkeypatch.setattr(f.base, "fetch_bands_from_skimmer", lambda _h, port, _c: sett[port])
    assert f.bands_of_ports([7301, 7302], "N0CALL-2") == [(7000.0, 7096.0), (14000.0, 14096.0)]


def test_another_receiver_s_copy_waits_the_whole_interval() -> None:
    r = f.Respot(120.0)
    assert r.allow("K1ABC", 7030.1, 0.0, "Lake-1")
    assert not r.allow("K1ABC", 7030.1, 117.0, "Lake-2")  # inside the slack: not its own re-spot
    assert r.allow("K1ABC", 7030.1, 117.0, "Lake-1")  # Lake-1's own re-spot, a little early
    assert not r.allow("K1ABC", 7030.2, 119.0, "Lake-2")


def test_a_short_interval_still_drops_another_receiver_s_copy() -> None:
    r = f.Respot(3.0)
    assert r.allow("K1ABC", 7030.1, 0.0, "Lake-1")
    assert not r.allow("K1ABC", 7030.1, 2.0, "Lake-2")
    assert r.allow("K1ABC", 7030.1, 1.0, "Lake-1")  # its own skimmer keeps its own interval
    assert r.allow("K1ABC", 7030.1, 6.0, "Lake-2")


@pytest.mark.parametrize("interval", [0, -5, "x", None, float("inf")])
def test_an_unusable_respot_interval_is_the_default(tmp_path: Path, interval: object) -> None:
    (tmp_path / "Lake-1.local.json").write_text(json.dumps({"respot_interval": interval}))
    assert f.receiver_settings(tmp_path, "Lake-1").respot_s == f.RESPOT_S


def test_each_reader_marks_its_spots_with_its_receiver() -> None:
    spots: queue.Queue[dict[str, Any]] = queue.Queue()

    def read(_host: str, _port: int, _call: str, out: Any, stop: threading.Event) -> None:
        out.put({"call": "K1ABC", "freq": 7030.1})
        stop.wait()

    readers = f.Readers("N0CALL-2", spots, threading.Event(), threading.Event(), f.Respot(), read)
    readers.set([_receiver("Lake-3", 7303)])
    assert spots.get(timeout=2) == {"call": "K1ABC", "freq": 7030.1, "rx": "Lake-3"}
    assert readers.names() == ["Lake-3"]
    readers.stop_all()
