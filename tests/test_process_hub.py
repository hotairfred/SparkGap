"""The server's /process hub (cdub89/SparkGap#33): a process of this machine links, says who it is,
sends its status and answers commands; its link is the server's view of whether it runs."""

import json
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from process_link import ProcessLink
from seat import Seat, SeatError
from sparkgap_server import Server
from tests.test_seat import seat  # noqa: F401  (the fixture)
from tests.test_server import server  # noqa: F401  (a running server on a free port)


def _until(cond: Callable[[], Any], wait_s: float = 5.0) -> Any:
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if got := cond():
            return got
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_a_process_links_and_sends_its_status(server: Server) -> None:  # noqa: F811
    link = ProcessLink("feeder", "dallas1", port=server.port).start()
    try:
        p = _until(lambda: server.procs.get(("feeder", "dallas1")))
        link.status(mode="dry", posts=3)
        _until(lambda: p.status.get("posts") == 3)
        assert p.status == {"mode": "dry", "posts": 3}
        snap = server._snapshot()  # pyright: ignore[reportPrivateUsage]
        assert [x["name"] for x in snap["processes"]] == ["dallas1"]
    finally:
        link.close()
    _until(lambda: ("feeder", "dallas1") not in server.procs)


def test_a_command_goes_down_and_its_answer_comes_back(server: Server) -> None:  # noqa: F811
    got: list[str] = []

    def on_command(cmd: str, args: dict[str, Any]) -> tuple[bool, str]:
        got.append(cmd)
        return True, f"{cmd} done"

    link = ProcessLink("feeder", "dallas2", on_command, port=server.port).start()
    try:
        _until(lambda: server.procs.get(("feeder", "dallas2")))
        assert server.command_sync("feeder", "dallas2", "stop") == (True, "stop done")
        assert got == ["stop"]
        assert server.command_sync("feeder", "nobody", "stop") is None
    finally:
        link.close()


def test_status_sent_before_the_link_reaches_the_server_arrives(server: Server) -> None:  # noqa: F811
    link = ProcessLink("station", "Dallas", port=server.port)
    link.status(state="live")
    link.start()
    try:
        p = _until(lambda: server.procs.get(("station", "Dallas")))
        _until(lambda: p.status.get("state") == "live")
    finally:
        link.close()


def test_without_the_process_protocol_the_hub_refuses(server: Server) -> None:  # noqa: F811
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as s:
        s.sendall(b"GET /process HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                  b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                  b"Sec-WebSocket-Version: 13\r\n\r\n")
        assert not s.makefile("rb").readline().startswith(b"HTTP/1.1 101")



def _feeder(server: Server, name: str) -> ProcessLink:  # noqa: F811
    """A stand-in feeder: a link that closes itself on stop, as rbn2_feeder does."""
    holder: list[ProcessLink] = []

    def on_command(cmd: str, _args: dict[str, Any]) -> tuple[bool, str]:
        if cmd == "stop":
            threading.Timer(0.1, holder[0].close).start()
            return True, "rbn feeder stopping"
        return False, "?"

    link = ProcessLink("feeder", name, on_command, port=server.port)
    holder.append(link)
    link.start()
    _until(lambda: server.procs.get(("feeder", name)))
    return link


def test_a_linked_feeder_is_stopped_through_its_link(server: Server) -> None:  # noqa: F811
    server.busy["dallas1"] = "testing"  # the sweep leaves a busy instance alone
    link = _feeder(server, "dallas1")
    try:
        assert server._feeder_down("dallas1") == "rbn feeder stopped"  # pyright: ignore[reportPrivateUsage]
        assert ("feeder", "dallas1") not in server.procs
    finally:
        link.close()


def test_the_feeder_of_a_copy_that_stopped_by_itself_is_stopped(server: Server) -> None:  # noqa: F811
    """dallas1 is not running (no copy): the hub's sweep stops its feeder (Dallas outage 11-02)."""
    link = _feeder(server, "dallas1")
    try:
        _until(lambda: ("feeder", "dallas1") not in server.procs, wait_s=10.0)
    finally:
        link.close()


def test_an_unknown_rbn_mode_is_refused(server: Server) -> None:  # noqa: F811
    with pytest.raises(SeatError):
        server._set_upload("dallas1", "loud")  # pyright: ignore[reportPrivateUsage]


def test_a_station_whose_radio_has_no_copy_running_is_stopped(server: Server) -> None:  # noqa: F811
    """No copy of radio 192.0.2.9 runs (a clean stop of one, a crash of the other): its station
    goes too (Dallas 11-02, SparkGap-Dallas left holding the radio)."""
    holder: list[ProcessLink] = []

    def on_command(cmd: str, _args: dict[str, Any]) -> tuple[bool, str]:
        threading.Timer(0.1, holder[0].close).start()
        return True, "station stopping"

    link = ProcessLink("station", "192.0.2.9", on_command, port=server.port)
    holder.append(link)
    link.start()
    try:
        _until(lambda: server.procs.get(("station", "192.0.2.9")))
        _until(lambda: ("station", "192.0.2.9") not in server.procs, wait_s=10.0)
    finally:
        link.close()


def test_a_linked_skimmer_runs_with_its_feed_port_and_receivers(server: Server) -> None:  # noqa: F811
    """The skimmer's link, not its pid file or log, says it runs and on which feed port."""
    rx = [{"band": "17m", "centre_khz": 18088.4, "rx": 0}]
    link = ProcessLink("skimmer", "dallas1", port=server.port)
    link.status(state="live", feed_port=7333, receivers=rx)
    link.start()
    try:
        _until(lambda: server.procs.get(("skimmer", "dallas1")))
        _until(lambda: server.procs[("skimmer", "dallas1")].status.get("feed_port"))
        snap = server._snapshot()  # pyright: ignore[reportPrivateUsage]
        d1 = next(i for i in snap["instances"] if i["name"] == "dallas1")
        got = (d1["state"], d1["linked"], d1["feed_port"], d1["receivers"])
        assert got == ("running", True, 7333, rx)
        assert d1["pid"] == os.getpid()  # from its hello: no pid file
        d2 = next(i for i in snap["instances"] if i["name"] == "dallas2")
        assert (d2["state"], d2["linked"]) == ("stopped", False)
        target = server._feed_target("dallas1")  # pyright: ignore[reportPrivateUsage]
        assert target is not None and target[1] == 7333
    finally:
        link.close()


def test_the_station_health_comes_from_its_link(server: Server) -> None:  # noqa: F811
    link = ProcessLink("station", "192.0.2.9", port=server.port)
    link.status(station="SparkGap-Dallas", state="live", rtt_ms=4, losses=[[time.time() - 60, 2.0]])
    link.start()
    skimmer = ProcessLink("skimmer", "dallas1", port=server.port).start()  # so the station is kept
    try:
        _until(lambda: server.procs.get(("station", "192.0.2.9")))
        _until(lambda: server.procs[("station", "192.0.2.9")].status)
        snap = server._snapshot()  # pyright: ignore[reportPrivateUsage]
        st = next(s for s in snap["stations"] if s["radio"] == "192.0.2.9")
        assert (st["state"], st["station"], st["live"], st["rtt_ms"], st["losses_hour"]) == (
            "running", "SparkGap-Dallas", "live", 4, 1)
    finally:
        skimmer.close()
        link.close()


def test_a_feeder_stays_while_its_skimmer_is_linked(server: Server) -> None:  # noqa: F811
    """No pid file, but the skimmer is linked: its feeder is not stopped."""
    skimmer = ProcessLink("skimmer", "dallas1", port=server.port).start()
    try:
        _until(lambda: server.procs.get(("skimmer", "dallas1")))
        feeder = _feeder(server, "dallas1")
        try:
            time.sleep(3)
            assert ("feeder", "dallas1") in server.procs
        finally:
            feeder.close()
    finally:
        skimmer.close()


def _on_radio(seat: Seat) -> Path:  # noqa: F811
    """dallas1 and dallas2 as receivers of radio Dallas, its node settings in its radio file."""
    for n in ("dallas1", "dallas2"):
        p = seat.root / "config" / f"{n}.local.json"
        p.write_text(json.dumps({**json.loads(p.read_text()), "radio": "Dallas"}))
    radio = seat.root / "config" / "Dallas.radio.json"
    radio.write_text(json.dumps({"type": "flex", "nickname": "Dallas", "address": "192.0.2.9",
                                 "callsign": "N0CALL-5", "grid": "EM12ou", "rbn_upload": "dry"}))
    return radio


def test_a_radio_s_receivers_share_one_feeder(seat: Seat, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    radio = _on_radio(seat)
    i = seat.instance("dallas1")
    assert (seat.feeder_name(i), seat.upload_mode(i)) == ("Dallas", "dry")
    argv: list[list[str]] = []
    monkeypatch.setattr(seat, "_launch", lambda _cwd, _pid, _log, a: argv.append(a))
    seat.spawn_feeder(i, ["dallas1", "dallas2"])
    assert argv[-1][1:] == ["rbn2_feeder.py", "--radio", str(radio), "--receiver", "dallas1",
                            "--receiver", "dallas2"]
    seat.set_upload(i, "live")
    st = json.loads(radio.read_text())
    assert st["rbn_upload"] == "live" and st["callsign"] == "N0CALL-5"
    seat.set_upload(i, None)
    assert "rbn_upload" not in json.loads(radio.read_text())
    assert seat.upload_mode(seat.instance("dallas2")) == "off"


def test_a_receiver_that_stops_leaves_the_radio_s_feeder_to_the_others(server: Server) -> None:  # noqa: F811
    _on_radio(server.seat)
    got: list[tuple[str, dict[str, Any]]] = []
    holder: list[ProcessLink] = []

    def on_command(cmd: str, args: dict[str, Any]) -> tuple[bool, str]:
        got.append((cmd, args))
        if cmd == "stop":
            threading.Timer(0.1, holder[0].close).start()
        return True, cmd

    skimmers = [ProcessLink("skimmer", n, port=server.port).start() for n in ("dallas1", "dallas2")]
    feeder = ProcessLink("feeder", "Dallas", on_command, port=server.port)
    holder.append(feeder)
    feeder.start()
    try:
        _until(lambda: all(("skimmer", n) in server.procs for n in ("dallas1", "dallas2")))
        _until(lambda: ("feeder", "Dallas") in server.procs)
        server._feeder_down("dallas1")  # pyright: ignore[reportPrivateUsage]
        assert got == [("receivers", {"names": ["dallas2"]})]
        assert server._feeder_up("dallas1") == "receivers"  # pyright: ignore[reportPrivateUsage]
        assert got[-1] == ("receivers", {"names": ["dallas1", "dallas2"]})
        skimmers[0].close()
        _until(lambda: ("skimmer", "dallas1") not in server.procs)
        assert server._feeder_down("dallas2") == "rbn feeder stopped"  # pyright: ignore[reportPrivateUsage]
        assert got[-1][0] == "stop"
    finally:
        for s in skimmers:
            s.close()
        feeder.close()


def test_a_radio_s_feeder_with_no_receiver_running_is_stopped(server: Server) -> None:  # noqa: F811
    _on_radio(server.seat)
    link = _feeder(server, "Dallas")
    try:
        _until(lambda: ("feeder", "Dallas") not in server.procs, wait_s=10.0)
    finally:
        link.close()


def test_the_snapshot_lists_each_radio_with_its_feeder(server: Server) -> None:  # noqa: F811
    _on_radio(server.seat)
    link = ProcessLink("feeder", "Dallas", port=server.port)
    link.status(mode="dry", registered=True, posts=2, receivers=["dallas1"])
    link.start()
    try:
        _until(lambda: (p := server.procs.get(("feeder", "Dallas"))) and p.status.get("posts"))
        snap = server._snapshot()  # pyright: ignore[reportPrivateUsage]
        (r,) = snap["radios"]
        assert {k: r[k] for k in ("name", "type", "address", "callsign", "rbn_upload")} == {
            "name": "Dallas", "type": "flex", "address": "192.0.2.9", "callsign": "N0CALL-5",
            "rbn_upload": "dry"}
        assert "antenna" in r["rx_settings"]
        assert r["feeder"]["posts"] == 2 and r["feeder"]["receivers"] == ["dallas1"]
        assert {i["radio"] for i in snap["instances"]} == {"Dallas"}
    finally:
        link.close()


def test_a_feeder_running_before_a_server_restart_is_not_started_twice(
        server: Server, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Its pid runs, its link is not back yet: wait for it, never spawn a second (Codex 11-02)."""
    import sparkgap_server

    _on_radio(server.seat)
    monkeypatch.setattr(sparkgap_server, "FEED_RELINK_S", 0.5)
    monkeypatch.setattr(server.seat, "feeder_pid", lambda _n: 4242)
    spawned: list[str] = []
    monkeypatch.setattr(server.seat, "spawn_feeder", lambda i, rx=None: spawned.append(i.name))
    skimmer = ProcessLink("skimmer", "dallas1", port=server.port).start()
    try:
        _until(lambda: ("skimmer", "dallas1") in server.procs)
        got = server._feeder_up("dallas1")  # pyright: ignore[reportPrivateUsage]
        assert got == "rbn feeder Dallas runs (pid 4242) but has not linked" and not spawned
    finally:
        skimmer.close()


def test_rbn_set_on_a_stopped_receiver_feeds_from_a_running_one(
        server: Server, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    import sparkgap_server

    _on_radio(server.seat)
    monkeypatch.setattr(sparkgap_server, "FEED_LINK_S", 0.2)
    spawned: list[tuple[str, list[str] | None]] = []

    def spawn(i: Any, rx: list[str] | None = None) -> Path:
        spawned.append((i.name, rx))
        return server.seat.logs / "rbn2_Dallas_test.log"

    monkeypatch.setattr(server.seat, "spawn_feeder", spawn)
    skimmer = ProcessLink("skimmer", "dallas1", port=server.port).start()
    try:
        _until(lambda: ("skimmer", "dallas1") in server.procs)
        server._set_upload("dallas2", "live")  # pyright: ignore[reportPrivateUsage]
        assert spawned == [("dallas1", ["dallas1"])]
    finally:
        skimmer.close()


def test_the_reaper_leaves_a_feeder_while_an_operation_runs(server: Server) -> None:  # noqa: F811
    _on_radio(server.seat)
    server.busy["dallas1"] = "start"
    link = _feeder(server, "Dallas")
    try:
        time.sleep(3)
        assert ("feeder", "Dallas") in server.procs
        del server.busy["dallas1"]
        _until(lambda: ("feeder", "Dallas") not in server.procs, wait_s=10.0)
    finally:
        link.close()
