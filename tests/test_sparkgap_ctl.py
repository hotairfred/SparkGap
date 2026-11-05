"""sparkgap_ctl.py drives the server as SparkGap Web does, and shows radio, receivers, feeder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sparkgap_ctl as ctl
from seat import Seat
from sparkgap_server import Server
from tests.test_seat import seat  # noqa: F401  (the fixture)
from tests.test_server import CODE, server  # noqa: F401

NOW = 1_800_000_000.0
SNAP = {
    "utc": NOW,
    "instances": [
        {"name": "Lake-1", "state": "running", "radio": "Lake", "band": "40m", "build": "abc1234",
         "started": NOW - 3720, "cpu": 31, "rss_mb": 410, "spots": 312, "errors": 0,
         "warnings": 2, "reconnects": 0, "telnet_port": 7301, "feed_port": 7322,
         "receivers": [{"band": "40m", "centre_khz": 7038.4, "rx": 0}],
         "top_calls": [["K1ABC", 4]], "log": "Lake-1_20261102_120000Z.log"},
        {"name": "Lake-4", "state": "stopped", "radio": "Lake", "band": "17m", "spots": 0,
         "errors": 1, "warnings": 0, "reconnects": 0, "telnet_port": 7304, "feed_port": None,
         "last_error": "ERROR radio gone"},
        {"name": "hp1", "state": "stopped", "radio": None, "band": "20m", "spots": 0,
         "errors": 0, "warnings": 0, "reconnects": 0, "telnet_port": 7310, "feed_port": None,
         "settings": {"rbn_upload": "off"}},
    ],
    "stations": [{"radio": "Lake", "state": "running", "live": "live", "rtt_ms": 3,
                  "losses_hour": 0}],
    "radios": [{"name": "Lake", "type": "flex", "model": "FLEX-6600", "address": "192.0.2.101",
                "callsign": "N0CALL-2", "rbn_upload": "dry",
                "feeder": {"mode": "dry", "spotter": "N0CALL-2", "registered": True, "posts": 41,
                           "sent": 940, "errors": 0, "receivers": ["Lake-1"]}}],
    "processes": [],
}


def test_status_shows_each_radio_its_node_and_a_row_per_receiver() -> None:
    lines = ctl.render(SNAP)
    assert lines[:3] == [
        "━━ Radio: Connected Lake ━━  FLEX-6600  192.0.2.101",
        "   node     N0CALL-2  rbn upload DRY",
        "   feeder   signed in"]
    head = next(x for x in lines if x.lstrip().startswith("receiver"))
    assert head.split() == ["receiver", "state", "band", "kHz", "spots", "err", "warn", "up",
                            "cpu", "mem", "build"]
    lake1 = next(x for x in lines if "Lake-1" in x)
    assert lake1.split() == ["Lake-1", "running", "40m", "7038.4", "312", "0", "2", "1h", "2m",
                             "31%", "410", "MB", "abc1234"]
    lake4 = lines.index(next(x for x in lines if "Lake-4" in x))
    assert lines[lake4].split()[:3] == ["Lake-4", "stopped", "17m"]
    assert lines[lake4 + 1] == "     last error: ERROR radio gone"
    assert "━━ receivers with no radio file ━━" in lines
    assert lines[-1] == "   hp1: feeder off"
    assert not any("\033[" in x for x in lines)  # plain when piped


def test_status_highlights_state_errors_and_labels_only() -> None:
    text = "\n".join(ctl.render(SNAP, color=True))
    assert "\033[32mrunning\033[0m" in text and "\033[31mstopped\033[0m" in text
    assert "\033[34mnode\033[0m" in text and "\033[34mreceiver\033[0m" in text
    assert "\033[33mDRY\033[0m" in text
    assert "\033[31m1\033[0m" in text  # Lake-4's error count
    assert "7038.4" in text and "\033[34m7038.4" not in text  # values stay plain


def test_a_radio_file_s_size_shows_on_its_line() -> None:
    snap = {**SNAP, "radios": [{**SNAP["radios"][0], "num_scu": 2, "max_panadapters": 4}]}
    assert "2 SCU  pans 1/4" in ctl.render(snap)[0]


def test_status_of_named_receivers_only() -> None:
    lines = ctl.render(SNAP, ["hp1"])
    assert lines[0].startswith("  hp1") and not any("Lake" in x for x in lines)


@pytest.mark.parametrize("cmd, args, want", [
    ("band", ["20m"], {"band": "20m"}),
    ("rec", ["on"], {"record": True}),
    ("rbn", ["dry"], {"rbn_upload": "dry"}),
    ("set", ["validation=normal", "afc=true", "spot_rule_strongest_db=null"],
     {"validation": "normal", "afc": True, "spot_rule_strongest_db": None}),
])
def test_commands_become_the_server_s_settings(cmd: str, args: list[str],
                                               want: dict[str, object]) -> None:
    assert ctl.settings_of(cmd, args) == want


@pytest.mark.parametrize("cmd, args", [("rec", ["maybe"]), ("rbn", ["on"]), ("set", ["afc"]),
                                       ("band", [])])
def test_a_wrong_command_says_its_usage(cmd: str, args: list[str]) -> None:
    with pytest.raises(ValueError, match="usage"):
        ctl.settings_of(cmd, args)


def test_no_server_json_is_no_server(tmp_path: Path) -> None:
    with pytest.raises(ctl.NoServer, match="server.json"):
        ctl.server_settings(tmp_path)
    (tmp_path / "server.json").write_text(json.dumps({"port": 1, "join_code": CODE}))
    assert ctl.server_settings(tmp_path) == (1, CODE, False)
    assert ctl.main(["--root", str(tmp_path), "status"]) == ctl.EXIT_NO_SERVER


def test_the_client_reads_the_state_and_changes_a_setting(server: Server) -> None:  # noqa: F811
    c = ctl.Client(server.port, CODE)
    try:
        snap = c.snapshot()
        assert [i["name"] for i in snap["instances"]] == ["dallas1", "dallas2"]
        assert c.request("set", "dallas2", {"validation": "normal"}) == (
            True, "dallas2: validation normal")
        ok, text = c.request("set", "dallas2", {"validation": "loud"})
        assert not ok and "validation must be one of" in text
    finally:
        c.close()
    cfg = json.loads((server.seat.root / "config" / "dallas2.local.json").read_text())
    assert cfg["spot_validation"] == "normal"


def test_a_wrong_join_code_is_refused(server: Server) -> None:  # noqa: F811
    c = ctl.Client(server.port, "WRONG1")
    try:
        with pytest.raises(ctl.NoServer):
            c.snapshot()
    finally:
        c.close()


def test_the_client_speaks_tls_when_the_server_does(seat: Seat, tmp_path: Path) -> None:  # noqa: F811
    import shutil
    import subprocess
    import threading

    from tests.test_server import _run

    if not shutil.which("openssl"):
        pytest.skip("no openssl to make a test certificate")
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj",
                    "/CN=test", "-days", "1", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    a = Server(seat.root, {"bind": "127.0.0.1", "port": 0, "join_code": CODE,
                           "tls_cert": str(cert), "tls_key": str(key)})
    threading.Thread(target=lambda: _run(a), daemon=True).start()
    assert a.ready.wait(5)
    c = ctl.Client(a.port, CODE, tls=True)
    try:
        assert [i["name"] for i in c.snapshot()["instances"]] == ["dallas1", "dallas2"]
    finally:
        c.close()


@pytest.mark.parametrize("feeder, mode, want", [
    (None, "off", "off"), (None, "dry", "not running"),
    ({"registered": True, "errors": 0}, "dry", "signed in"),
    ({"registered": False, "errors": 0}, "live", "not signed in"),
    ({"registered": True, "errors": 3}, "live", "3 errors"),
])
def test_the_feeder_line_is_its_state_alone(feeder: dict[str, object] | None, mode: str,
                                             want: str) -> None:
    assert ctl.feeder_text(feeder, mode, ctl.Paint(False)) == want


@pytest.mark.parametrize("live, want", [
    ("live", "Connected"), (None, "Connecting"), ("reconnecting", "Reconnecting"),
    ("connecting", "Connecting"), ("halted", "Not connected"), ("stopped", "Not connected"),
])
def test_a_running_station_shows_the_radios_own_state(live: str | None, want: str) -> None:
    # Dallas 11-05: the radio rebooted while its station ran, and the bar still said Connected
    assert ctl.connection({"radio": "Lake", "state": "running", "live": live}) == want


def test_a_stopped_station_is_a_radio_not_connected() -> None:
    snap = {**SNAP, "stations": [{"radio": "Lake", "state": "stopped"}]}
    assert ctl.render(snap)[0].startswith("━━ Radio: Not connected Lake ━━")


def test_a_recording_receiver_says_so() -> None:
    lake1 = {**SNAP["instances"][0], "settings": {"record": True}}
    snap = {**SNAP, "instances": [lake1, *SNAP["instances"][1:]]}
    row = next(x for x in ctl.render(snap) if "Lake-1" in x)
    assert row.split()[:3] == ["Lake-1", "running,", "rec"]


def test_a_radio_with_no_receivers_yet_is_shown() -> None:
    snap = {"utc": NOW, "instances": [], "stations": [], "radios": [], "processes": [],
            "radio_files": [{"name": "Dallas", "type": "flex", "model": "FLEX-6400M",
                             "address": "192.0.2.150", "callsign": "N0CALL-5", "grid": "EM12ou",
                             "rbn_upload": "dry"}]}
    lines = ctl.render(snap)
    assert lines[0].startswith("━━ Radio: Dallas ━━  FLEX-6400M  192.0.2.150")
    assert lines[-1] == "   no receivers yet: add them in SparkGap Web"
