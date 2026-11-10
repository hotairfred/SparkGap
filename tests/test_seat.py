"""seat.py runs a machine's SparkGap instances: real processes from
stand-in sparkgap.py and flex_station.py scripts in a temporary root."""

import json
import socket
import os
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest

import seat as seat_mod
from seat import Seat, SeatError, write_config

COPY = """import signal, sys, time
print("12:00:00 INFO GUI feed on ws://127.0.0.1:7321/feed, join code ABC-DEF", flush=True)
print("12:00:01 INFO SparkGap LIVE: 40m (7038.400 kHz) rx0, telnet :7300", flush=True)
print("12:00:02 INFO *** SPOT:   7030.00  K8TLJ         12 dB  20 WPM  [repeat] ***", flush=True)
print("12:00:03 INFO ITILA raw 7030.0 kHz cost=0.40: 'CQ K8TLJ K8TLJ K'", flush=True)
signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
while True:
    time.sleep(0.1)
"""
STATION = """import signal, sys, time
print("12:00:00 INFO [Flex] Station SparkGap-Test holds the GUI client (client 0x1)", flush=True)
signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
while True:
    time.sleep(0.1)
"""
BANDS_LINE = "BANDS = dict(zip(('40m', '20m', '17m'), (7000, 14000, 18068)))\n"


@pytest.fixture
def seat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Seat]:
    code = tmp_path / "code"
    code.mkdir()
    (code / "sparkgap.py").write_text(COPY + BANDS_LINE)
    (code / "flex_station.py").write_text(STATION)
    (tmp_path / "config").mkdir()
    (tmp_path / "logs").mkdir()
    for n, band in (("dallas1", "17m"), ("dallas2", "40m")):
        cfg = {
            "sdr_type": "flex",
            "sdr_ip": "192.0.2.9",
            "bands": [band],
            "gui_feed": {"bind": "127.0.0.1", "port": "auto", "join_code": "ABCDEF"},
        }
        (tmp_path / "config" / f"{n}.local.json").write_text(json.dumps(cfg, indent=2) + "\n")
    lines = [f"{n} {code} {n}.local.json python3" for n in ("dallas1", "dallas2")]
    (tmp_path / "instances").write_text("# name code-dir config python\n" + "\n".join(lines) + "\n")
    monkeypatch.setattr(seat_mod, "START_WAIT_S", 10)
    s = Seat(tmp_path)
    yield s
    for pidfile in (tmp_path / "logs").glob("*.pid"):
        try:
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def test_one_station_for_two_copies_stopped_after_the_last(seat: Seat) -> None:
    assert seat.start("dallas1").startswith("dallas1: live")
    assert seat.start("dallas2").startswith("dallas2: live")
    assert len(list(seat.logs.glob("station_192.0.2.9_*.log"))) == 1
    seat.stop("dallas1")
    assert seat.station_pid("192.0.2.9") is not None
    seat.stop("dallas2")
    assert seat.station_pid("192.0.2.9") is None and not list(seat.logs.glob("*.pid"))


def test_a_copy_keeps_running_when_its_starter_is_gone(seat: Seat) -> None:
    seat.start("dallas1")
    pid = seat.pid("dallas1")
    assert pid is not None and os.getsid(pid) != os.getsid(0)  # its own session, not ours


def test_status_reads_the_log_config_and_station(seat: Seat) -> None:
    seat.start("dallas1")
    snap = seat.snapshot()
    d1 = next(i for i in snap["instances"] if i["name"] == "dallas1")
    assert d1["state"] == "running" and d1["band"] == "17m" and d1["feed_port"] == 7321
    assert d1["feed_code"] == "ABCDEF" and d1["spots"] == 1 and d1["top_calls"] == [("K8TLJ", 1)]
    assert snap["stations"] == [{"radio": "192.0.2.9", "state": "running"}]  # health: its link


def test_a_setting_is_written_and_a_running_copy_restarted(seat: Seat) -> None:
    seat.start("dallas2")
    before = seat.pid("dallas2")
    out = seat.change("dallas2", {"validation": "normal", "call_source": "rbn+skcc"})
    cfg = json.loads((seat.root / "config" / "dallas2.local.json").read_text())
    assert cfg["spot_validation"] == "normal" and cfg["spot_validation_calls"] == "rbn+skcc"
    assert "live" in out and seat.pid("dallas2") not in (None, before)


def test_a_multi_band_copy_keeps_its_bands(seat: Seat) -> None:
    """hotairfred/SparkGap#94 review: a band change wrote a multi-band config down to one band."""
    i = seat.instance("dallas1")
    cfg = json.loads(i.config.read_text())
    i.config.write_text(json.dumps({**cfg, "bands": ["40m", "20m"]}))
    before = i.config.read_text()
    assert seat.band(i) == "40m+20m" and seat.settings(i)["multi_band"] is True
    with pytest.raises(SeatError, match="dallas1 runs 40m\\+20m: set its bands in its config"):
        seat.change("dallas1", {"band": "17m"})
    assert i.config.read_text() == before
    assert seat.settings(seat.instance("dallas2"))["multi_band"] is False


def test_afc_is_written_to_the_config(seat: Seat) -> None:
    i = seat.instance("dallas1")
    assert seat.change("dallas1", {"afc": True}) == "dallas1: afc True"
    assert seat.config(i)["itila2_afc"] is True and seat.settings(i)["afc"] is True
    assert seat.settings(i)["decoder"] == "itila2"


def test_decoder_options_are_written_and_off_removes_the_key(seat: Seat) -> None:
    i = seat.instance("dallas1")
    seat.change("dallas1", {"spot_rule_owner_cell": True, "spot_rule_strongest_db": 6})
    assert seat.config(i)["spot_rule_owner_cell"] is True
    assert seat.settings(i)["spot_rule_strongest_db"] == 6
    seat.change("dallas1", {"spot_rule_strongest_db": None})
    assert "spot_rule_strongest_db" not in seat.config(i)
    assert seat.settings(i)["spot_rule_strongest_db"] is None


def test_the_decode_window_reads_its_default_and_takes_listed_lengths(seat: Seat) -> None:
    i = seat.instance("dallas1")
    assert seat.settings(i)["itila_window_sec"] == seat.config(i).get("itila_window_sec", 60)
    seat.change("dallas1", {"itila_window_sec": 7.5})
    assert seat.config(i)["itila_window_sec"] == 7.5
    with pytest.raises(SeatError, match="itila_window_sec must be one of: 60, 30, 15, 7.5"):
        seat.change("dallas1", {"itila_window_sec": 10})


def test_a_stopped_copy_is_only_written(seat: Seat) -> None:
    assert seat.change("dallas1", {"band": "20m"}) == "dallas1: band 20m"
    assert seat.pid("dallas1") is None and seat.band(seat.instance("dallas1")) == "20m"


@pytest.mark.parametrize(
    "settings, error",
    [
        ({"callsign": "N0CALL"}, "cannot be changed"),
        ({"rbn_auth": "x"}, "cannot be changed"),
        ({"band": "6m"}, "band must be one of: 40m 20m 17m"),
        ({"validation": "loose"}, "validation must be one of"),
        ({"spot_rule_owner_cell": "yes"}, "true or false"),
        ({"spot_rule_split_runs": True}, "cannot be changed"),          # built in (cdub89/SparkGap#46)
        ({"afc": "on"}, "afc must be true or false"),
        ({"spot_rule_strongest_db": 4}, "strongest_db must be one of: off, 3"),
        ({"spot_rule_takeover_idle_s": 180}, "cannot be changed"),      # built in
        ({"spot_rule_strongest_db": True}, "strongest_db must be one of"),
        ({}, "nothing to change"),
    ],
)
def test_anything_off_the_list_is_refused_and_nothing_written(
    seat: Seat, settings: dict[str, object], error: str
) -> None:
    path = seat.root / "config" / "dallas1.local.json"
    before = path.read_text()
    with pytest.raises(SeatError, match=error):
        seat.change("dallas1", settings)
    assert path.read_text() == before


def test_recording_needs_free_space(seat: Seat, monkeypatch: pytest.MonkeyPatch) -> None:
    usage = seat_mod.shutil._ntuple_diskusage(0, 0, 2 * 2**30)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(seat_mod.shutil, "disk_usage", lambda _p: usage)
    with pytest.raises(SeatError, match="only 2 GB free"):
        seat.change("dallas1", {"record": True})


def test_recording_on_and_off_sets_the_template(seat: Seat) -> None:
    seat.change("dallas1", {"record": True})
    cfg = json.loads((seat.root / "config" / "dallas1.local.json").read_text())
    assert cfg["record_wav"] == str(seat.rec / "dallas1_{band}_{ts}.wav")
    seat.change("dallas1", {"record": False})
    assert "record_wav" not in json.loads((seat.root / "config" / "dallas1.local.json").read_text())


def test_an_unknown_or_odd_name_is_refused(seat: Seat) -> None:
    with pytest.raises(SeatError, match="unknown instance"):
        seat.start("../etc")


def test_write_config_keeps_indentation(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text('{\n    "a": 1,\n    "b": 2\n}\n')
    write_config(path, {"a": None, "c": [1]})
    assert path.read_text() == '{\n    "b": 2,\n    "c": [1]\n}\n'


def test_write_config_keeps_the_files_own_layout(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    feed = '  "gui_feed": {"bind": "127.0.0.1",\n               "port": "auto"}'
    text = '{\n  "bands": ["40m"],\n' + feed + ',\n  "rec": null\n}\n'
    path.write_text(text)
    write_config(path, {"bands": ["20m"], "rec": None})
    assert path.read_text() == '{\n  "bands": ["20m"],\n' + feed + "\n}\n"
    write_config(path, {"gui_feed": {"port": 7321}})  # over several lines: written whole
    assert json.loads(path.read_text()) == {"bands": ["20m"], "gui_feed": {"port": 7321}}


def test_a_config_read_follows_its_file(seat: Seat) -> None:
    i = seat.instance("dallas1")
    assert seat.band(i) == "17m"
    write_config(i.config, {"bands": ["20m"]})
    assert seat.band(i) == "20m"


def test_a_reused_pid_is_never_signalled(seat: Seat) -> None:
    (seat.logs / "dallas1.pid").write_text(str(os.getpid()))  # now some other program
    assert seat.pid("dallas1") is None
    assert seat.stop("dallas1") == "dallas1: not running"  # and we are still here


def test_a_radio_address_with_path_characters_is_refused(seat: Seat) -> None:
    path = seat.root / "config" / "dallas1.local.json"
    write_config(path, {"sdr_ip": "../../etc/x"})
    with pytest.raises(SeatError, match="not a host name or address"):
        seat.start("dallas1")


def test_a_start_waits_for_the_lock_sparkgap_sh_holds(seat: Seat) -> None:
    import subprocess
    import time

    held = subprocess.Popen(["flock", str(seat.logs / "dallas1.lock"), "sleep", "2"])
    time.sleep(0.3)
    t0 = time.monotonic()
    seat.start("dallas1")
    held.wait()
    assert time.monotonic() - t0 >= 1.5


def test_a_feed_that_failed_to_start_has_no_port(seat: Seat) -> None:
    copy = seat.root / "code" / "sparkgap.py"
    copy.write_text(
        copy.read_text().replace(
            "GUI feed on ws://127.0.0.1:7321/feed, join code ABC-DEF",
            "GUI feed failed to start on 0.0.0.0:7320: [Errno 98] address already in use",
        )
    )
    seat.start("dallas1")
    d1 = seat.status(seat.instance("dallas1"))
    assert d1["feed_port"] is None and d1["feed_failed"] is True


def test_uptime_comes_from_the_process_not_the_log(seat: Seat) -> None:
    import time

    seat.start("dallas1")
    started = seat.status(seat.instance("dallas1"))["started"]
    assert started is not None and 0 <= time.time() - started < 30
    time.sleep(1.2)
    (seat.logs / seat.status(seat.instance("dallas1"))["log"]).open("a").write(
        "12:00:03 INFO more\n"
    )
    assert (
        seat.status(seat.instance("dallas1"))["started"] == started
    )  # a log write does not move it


def test_build_is_read_from_git_files(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "f").write_text("x")
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "f"], check=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    assert Seat.build(tmp_path) == sha[:7]
    subprocess.run([*git, "checkout", "-q", "--detach"], check=True)
    assert Seat.build(tmp_path) == sha[:7]
    assert Seat.build(tmp_path / "nowhere") is None


def test_status_from_many_threads_counts_each_spot_once(seat: Seat) -> None:
    """The server reads status on several threads; counts read on from an offset (2026-10-22)."""
    import threading

    seat.start("dallas1")
    log = seat.logs / seat.status(seat.instance("dallas1"))["log"]
    with log.open("a") as f:
        for _ in range(2_000):
            f.write("12:00:03 INFO *** SPOT:   18080.00  K8TLJ  12 dB  20 WPM  [repeat] ***\n")
    seat._stats.clear()  # pyright: ignore[reportPrivateUsage]
    d1 = seat.instance("dallas1")
    threads = [threading.Thread(target=seat.status, args=(d1,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seat.status(seat.instance("dallas1"))["spots"] == 2_001


def _cfg(seat: Seat, name: str) -> dict[str, object]:
    return json.loads((seat.root / "config" / f"{name}.local.json").read_text())


def test_a_running_copy_changes_its_receive_path_on_the_radio_only(seat: Seat) -> None:
    seat.start("dallas2")
    pid = seat.pid("dallas2")
    asked: list[dict[str, object]] = []

    def live_rx(name: str, rx: dict[str, object]) -> tuple[bool, str]:
        asked.append(rx)
        return True, "receive path rxant=ANT2 loopa=1 rfgain=16"

    out = seat.change(
        "dallas2", {"antenna": "ANT2", "loop_a": True, "rf_gain": 16}, live_rx=live_rx
    )
    assert asked == [{"rxant": "ANT2", "loopa": True, "rfgain": 16}]
    assert (
        out == "dallas2: receive path rxant=ANT2 loopa=1 rfgain=16" and seat.pid("dallas2") == pid
    )
    cfg = _cfg(seat, "dallas2")  # startup values change only while stopped
    assert not {"flex_rxant", "flex_loopa", "flex_rfgain"} & set(cfg)


@pytest.mark.parametrize(
    "answer, error",
    [((False, "the radio refused rxant=ANT2"), "refused"), (None, "did not answer")],
)
def test_a_receive_path_the_radio_did_not_take_changes_nothing(
    seat: Seat, answer: tuple[bool, str] | None, error: str
) -> None:
    seat.start("dallas2")
    pid = seat.pid("dallas2")
    with pytest.raises(SeatError, match=error):
        seat.change("dallas2", {"antenna": "ANT2"}, live_rx=lambda _n, _rx: answer)
    assert "flex_rxant" not in _cfg(seat, "dallas2") and seat.pid("dallas2") == pid


def test_a_stopped_copy_gets_its_receive_path_as_the_start_target(seat: Seat) -> None:
    assert (
        seat.change("dallas1", {"loop_b": True}, live_rx=lambda *_: None) == "dallas1: loop_b True"
    )
    assert _cfg(seat, "dallas1")["flex_loopb"] is True


@pytest.mark.parametrize(
    "settings, error",
    [
        ({"antenna": "ANT 1; x"}, "antenna must be"),
        ({"loop_a": 1}, "true or false"),
        ({"rf_gain": "16"}, "whole number"),
        ({"rf_gain": True}, "whole number"),
    ],
)
def test_a_receive_path_value_of_the_wrong_kind_is_refused(
    seat: Seat, settings: dict[str, object], error: str
) -> None:
    with pytest.raises(SeatError, match=error):
        seat.change("dallas1", settings)
    assert not any(k.startswith("flex_") for k in _cfg(seat, "dallas1"))


def test_an_hpsdr_receiver_shows_under_its_radio_and_gets_no_station(seat: Seat) -> None:
    i = seat.instance("dallas1")
    write_config(i.config, {"sdr_type": "hpsdr", "radio": "Web888"})
    (seat.root / "config" / "Web888.radio.json").write_text(json.dumps(
        {"type": "hpsdr", "model": "Web-888", "address": "192.0.2.120", "board_id": 6,
         "receivers": 6, "callsign": "N0CALL-1", "grid": "EM12ub"}))
    assert seat.radio(i) is None  # no Flex station for it
    assert seat.status(i)["radio"] == "Web888"
    snap = seat.snapshot()
    (r,) = snap["radios"]
    assert (r["name"], r["model"], r["board_id"], r["receivers"], r["rx_settings"]) == (
        "Web888", "Web-888", 6, 6, [])
    assert "192.0.2.120" not in {s["radio"] for s in snap["stations"]}


def test_an_hpsdr_radio_lists_one_receiver_per_band_with_its_spots(seat: Seat) -> None:
    i = seat.instance("dallas1")
    write_config(i.config, {"sdr_type": "hpsdr", "radio": "Web888", "sample_rate": 192_000,
                            "bands": ["40m", "20m"]})
    assert [r["name"] for r in seat.status(i)["receiver_list"]] == ["Web888-1", "Web888-2"]
    seat.start("dallas1")  # its log: 40m live on rx0 at 7038.4 kHz, one spot at 7030.0
    r1, r2 = seat.status(i)["receiver_list"]
    assert (r1["receiver"], r1["band"], r1["centre_khz"], r1["spots"]) == (1, "40m", 7038.4, 1)
    assert (r1["calls"], r1["windows"]) == (1, 1)  # its calls and decoded windows, from the log
    assert (r2["receiver"], r2["band"], r2["centre_khz"], r2["spots"], r2["windows"]) == (
        2, "20m", None, None, None)
    assert seat.status(seat.instance("dallas2"))["receiver_list"] is None  # a Flex copy


def test_an_hpsdr_receiver_moves_band_alone(seat: Seat) -> None:
    i = seat.instance("dallas1")
    write_config(i.config, {"sdr_type": "hpsdr", "radio": "Web888", "bands": ["40m", "20m"]})
    assert "rx_bands" in seat.change("dallas1", {"rx_bands": {"2": "17m"}})
    assert _cfg(seat, "dallas1")["bands"] == ["40m", "17m"]
    for value, error in (({"3": "17m"}, "receivers 1 to 2"), ({"2": "40m"}, "two receivers on one band"),
                         ({"1": "6m"}, "band must be one of"), ({}, "receiver number: band"),
                         ({"x": "17m"}, "receivers 1 to 2")):
        with pytest.raises(SeatError, match=error):
            seat.change("dallas1", {"rx_bands": value})
    assert _cfg(seat, "dallas1")["bands"] == ["40m", "17m"]
    with pytest.raises(SeatError, match="only an HPSDR radio's receivers"):
        seat.change("dallas2", {"rx_bands": {"1": "20m"}})
    seat.start("dallas1")
    pid = seat.pid("dallas1")
    assert "dallas1: live" in seat.change("dallas1", {"rx_bands": {"1": "20m"}})  # all its receivers restart
    assert seat.pid("dallas1") not in (None, pid) and _cfg(seat, "dallas1")["bands"] == ["20m", "17m"]


def test_a_flex_station_is_named_by_its_radio(seat: Seat) -> None:
    """radio (the nickname Discover found) names the station; without it, sdr_ip."""
    i = seat.instance("dallas1")
    write_config(i.config, {"radio": "Dallas"})
    assert seat.radio(i) == "Dallas"
    write_config(i.config, {"radio": None})
    assert seat.radio(i) == "192.0.2.9"


def with_radio(seat: Seat, monkeypatch: pytest.MonkeyPatch) -> Seat:
    """The seat with a Flex radio file and shipped defaults; added receivers run the stand-in code."""
    monkeypatch.setattr(seat_mod, "CODE", seat.root / "code")
    monkeypatch.setattr(seat_mod, "_port_free", lambda port, **_: True)  # not this machine's ports
    radio = {"type": "flex", "model": "FLEX-6400M", "callsign": "N0CALL-5", "grid": "EM12ou",
             "max_panadapters": 2, "max_slices": 2, "daxiq_channels": 2}
    (seat.root / "config" / "Dallas.radio.json").write_text(json.dumps(radio))
    template = {"sdr_type": "flex", "sdr_ip": "192.0.2.10", "callsign": "N0CALL", "skimmer_suffix": "-1",
                "telnet_port": 7300, "flex_udp_port": 7791, "flex_daxiq_channel": 2, "bands": ["20m"],
                "gui_feed": {"bind": "127.0.0.1", "port": 7321}}
    (seat.root / "code" / "skimmer_flex.example.json").write_text(json.dumps(template))
    return seat


def with_hpsdr(seat: Seat, monkeypatch: pytest.MonkeyPatch, receivers: int = 2) -> Seat:
    """The seat with a Web-888 radio file and the shipped HPSDR defaults."""
    with_radio(seat, monkeypatch)
    radio = {"type": "hpsdr", "model": "Web-888", "address": "192.0.2.120", "board_id": 6,
             "firmware": 73, "receivers": receivers, "callsign": "N0CALL-1", "grid": "EM12ub"}
    (seat.root / "config" / "Web888.radio.json").write_text(json.dumps(radio))
    template = {"sdr_type": "hpsdr", "sdr_ip": "192.0.2.10", "callsign": "N0CALL", "sample_rate": 192_000,
                "max_receivers": 6, "hpsdr_listen_port": 1024, "ppm_offset": 0.0, "telnet_port": 7330,
                "bands": ["20m"], "gui_feed": {"bind": "127.0.0.1", "port": 7321}}
    (seat.root / "code" / "skimmer_hpsdr.example.json").write_text(json.dumps(template))
    return seat


def test_an_hpsdr_radio_s_first_receiver_makes_its_process(seat: Seat, monkeypatch: pytest.MonkeyPatch) -> None:
    s = with_hpsdr(seat, monkeypatch)
    assert s.add("Web888-1", "20m").startswith("Web888-1: added, 20m; Web888 runs it")
    cfg = _cfg(s, "Web888")
    assert (cfg["radio"], cfg["sdr_type"], cfg["bands"], cfg["max_receivers"]) == ("Web888", "hpsdr", ["20m"], 2)
    assert (cfg["ppm_offset"], cfg["hpsdr_listen_port"], cfg["telnet_port"]) == (3.9, 1024, 7330)
    assert not {"callsign", "grid", "sdr_ip"} & set(cfg) and cfg["gui_feed"]["port"] == "auto"
    assert s.instance("Web888").config.name == "Web888.local.json"
    assert s.process_name("Web888-1") == "Web888" and s.process_name("dallas1") == "dallas1"
    assert s.add("Web888-2", "17m") == "Web888-2: added, 17m; used at Web888's next start"
    assert _cfg(s, "Web888")["bands"] == ["20m", "17m"]
    for name, band, error in (("Web888-3", "40m", "receivers 1 to 2"), ("Web888-5", "40m", "next receiver"),
                              ("Web888-2", "20m", "next receiver")):
        with pytest.raises(SeatError, match=error):
            s.add(name, band)


def test_an_hpsdr_receiver_added_to_a_running_radio_restarts_it(
        seat: Seat, monkeypatch: pytest.MonkeyPatch) -> None:
    s = with_hpsdr(seat, monkeypatch, receivers=6)
    s.add("Web888-1", "20m")
    s.start("Web888")
    pid = s.pid("Web888")
    with pytest.raises(SeatError, match="already has a receiver on 20m"):
        s.add("Web888-2", "20m")
    assert "Web888: live" in s.add("Web888-2", "17m")
    assert s.pid("Web888") not in (None, pid)


def test_an_hpsdr_receiver_is_removed_and_the_later_ones_move_up(
        seat: Seat, monkeypatch: pytest.MonkeyPatch) -> None:
    s = with_hpsdr(seat, monkeypatch, receivers=6)
    for n, band in enumerate(("20m", "17m", "40m"), 1):
        s.add(f"Web888-{n}", band)
    with pytest.raises(SeatError, match="receivers 1 to 3"):
        s.remove("Web888-4")
    assert s.remove("Web888-2") == "Web888-2: removed (17m); Web888-3 to -3 are now Web888-2 to -2"
    assert _cfg(s, "Web888")["bands"] == ["20m", "40m"]
    s.start("Web888")
    pid = s.pid("Web888")
    assert "Web888: live" in s.remove("Web888-2")  # a running radio restarts with the rest
    assert s.pid("Web888") not in (None, pid) and _cfg(s, "Web888")["bands"] == ["20m"]
    with pytest.raises(SeatError, match="running; stop it first"):
        s.remove("Web888-1")  # the last one takes the process: stopped first
    s.stop("Web888")
    assert s.remove("Web888-1").startswith("Web888-1: removed, Web888's last receiver")
    assert all(i.name != "Web888" for i in s.instances()) and (s.root / "config" / "Web888.local.json").exists()


def test_a_radio_with_no_receivers_is_removed(seat: Seat, monkeypatch: pytest.MonkeyPatch) -> None:
    s = with_hpsdr(seat, monkeypatch)
    s.add("Web888-1", "20m")
    with pytest.raises(SeatError, match="still has Web888: remove them first"):
        s.remove_radio("Web888")
    s.remove("Web888-1")
    for radio, error in (("../x", "not a radio name"), ("Nope", "no radio file")):
        with pytest.raises(SeatError, match=error):
            s.remove_radio(radio)
    assert s.remove_radio("Web888").startswith("Web888: removed; its radio file stays as config/Web888.radio.json.removed-")
    assert not (s.root / "config" / "Web888.radio.json").exists()
    assert [p.name for p in (s.root / "config").glob("Web888.radio.json.removed-*")]


def test_an_hpsdr_radio_never_takes_a_receiver_s_name(seat: Seat, monkeypatch: pytest.MonkeyPatch) -> None:
    s = with_hpsdr(seat, monkeypatch)
    clash = {"type": "hpsdr", "address": "192.0.2.122", "callsign": "N0CALL-8", "grid": "EM12ub"}
    (s.root / "config" / "dallas1.radio.json").write_text(json.dumps(clash))
    with pytest.raises(SeatError, match="dallas1 is already a receiver's name"):
        s.add("dallas1-1", "20m")
    s.add("Dallas-1", "40m")  # a Flex receiver <radio>-<n>
    with pytest.raises(SeatError, match="already used by Dallas-1"):
        s.discover(name="Dallas")
    (s.root / "config" / "Dallas.radio.json").write_text(json.dumps({**clash, "address": "192.0.2.123"}))
    with pytest.raises(SeatError, match="would share names with Dallas-1"):
        s.add("Dallas-1", "20m")  # Dallas is now an HPSDR radio file: its process would be Dallas


def test_a_second_hpsdr_radio_gets_its_own_listen_port(seat: Seat, monkeypatch: pytest.MonkeyPatch) -> None:
    s = with_hpsdr(seat, monkeypatch)
    s.add("Web888-1", "20m")
    pitaya = {"type": "hpsdr", "model": "HPSDR board 1", "address": "192.0.2.121", "receivers": 8,
              "callsign": "N0CALL-7", "grid": "EM12ub"}
    (s.root / "config" / "Pitaya.radio.json").write_text(json.dumps(pitaya))
    s.add("Pitaya-1", "40m")
    cfg = _cfg(s, "Pitaya")
    assert (cfg["hpsdr_listen_port"], cfg["telnet_port"], cfg["ppm_offset"]) == (1025, 7331, 0.0)


def test_an_hpsdr_radio_is_renamed_while_stopped(seat: Seat, monkeypatch: pytest.MonkeyPatch) -> None:
    s = with_hpsdr(seat, monkeypatch)
    s.add("Web888-1", "20m")
    s.start("Web888")
    with pytest.raises(SeatError, match="stop Web888 first"):
        s.rename_radio("Web888", "Pitaya")
    s.stop("Web888")
    for new, error in (("Dallas", "another radio's name"), ("Pit-2", "names a receiver"), ("../x", "letters")):
        (s.root / "config" / "Dallas.radio.json").touch()
        with pytest.raises(SeatError, match=error):
            s.rename_radio("Web888", new)
    with pytest.raises(SeatError, match="takes its name from the radio"):
        s.rename_radio("Dallas", "Shack")
    with pytest.raises(SeatError, match="not a radio name"):
        s.rename_radio("../../x", "Shack")  # never a file outside config/
    with pytest.raises(SeatError, match="already used by dallas1"):
        s.rename_radio("Web888", "dallas1")  # a receiver's name
    assert s.rename_radio("Web888", "Lakeside").startswith("Web888 renamed Lakeside")
    assert not (s.root / "config" / "Web888.radio.json").exists()
    assert json.loads((s.root / "config" / "Lakeside.radio.json").read_text())["nickname"] == "Lakeside"
    assert _cfg(s, "Lakeside")["radio"] == "Lakeside" and s.instance("Lakeside").config.name == "Lakeside.local.json"
    assert [r["name"] for r in s.status(s.instance("Lakeside"))["receiver_list"]] == ["Lakeside-1"]


@pytest.fixture
def radio_seat(seat: Seat, monkeypatch: pytest.MonkeyPatch) -> Seat:
    return with_radio(seat, monkeypatch)


def test_add_writes_a_receiver_that_runs_the_servers_code(radio_seat: Seat) -> None:
    assert radio_seat.add("Dallas-1", "40m") == "Dallas-1: added on DAX-IQ 1, 40m, from the shipped defaults"
    cfg = _cfg(radio_seat, "Dallas-1")
    assert (cfg["radio"], cfg["flex_daxiq_channel"], cfg["bands"]) == ("Dallas", 1, ["40m"])
    assert not {"callsign", "grid", "skimmer_suffix"} & set(cfg)  # its radio file's
    assert "sdr_ip" not in cfg and "flex_rxant" not in cfg and cfg["gui_feed"]["port"] == "auto"
    i = radio_seat.instance("Dallas-1")
    assert i.code == radio_seat.root / "code" and i.config.name == "Dallas-1.local.json"
    assert (cfg["telnet_port"], cfg["flex_udp_port"]) == (7331, 7792)  # 7791 is dallas1's default
    radio_seat.add("Dallas-2", "20m")
    two = _cfg(radio_seat, "Dallas-2")
    assert (two["telnet_port"], two["flex_udp_port"]) == (7332, 7793)


@pytest.mark.parametrize(
    "name, band, error",
    [
        ("Dallas", "40m", "<radio>-<n>"),
        ("Dallas-1", "6m", "band must be"),
        ("Lake-1", "40m", "no radio file"),
        ("Dallas-3", "40m", "channels 1 to 2"),
    ],
)
def test_add_refuses_what_it_cannot_make(radio_seat: Seat, name: str, band: str, error: str) -> None:
    with pytest.raises(SeatError, match=error):
        radio_seat.add(name, band)
    assert all(i.name != name for i in radio_seat.instances())


def test_add_refuses_a_full_radio_and_a_name_in_use(radio_seat: Seat) -> None:
    path = radio_seat.root / "config" / "Dallas.radio.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "daxiq_channels": 4}))
    radio_seat.add("Dallas-1", "40m")
    with pytest.raises(SeatError, match="already exists"):
        radio_seat.add("Dallas-1", "20m")
    radio_seat.add("Dallas-2", "20m")
    with pytest.raises(SeatError, match="holds 2 receivers"):
        radio_seat.add("Dallas-3", "17m")


def test_remove_keeps_the_config_and_adding_again_reuses_it(radio_seat: Seat) -> None:
    radio_seat.add("Dallas-1", "40m")
    write_config(radio_seat.instance("Dallas-1").config, {"spot_validation": "normal"})
    assert radio_seat.remove("Dallas-1") == "Dallas-1: removed; its config stays in config/Dallas-1.local.json"
    assert all(i.name != "Dallas-1" for i in radio_seat.instances())
    assert radio_seat.add("Dallas-1", "20m").endswith("from its earlier config")
    cfg = _cfg(radio_seat, "Dallas-1")
    assert cfg["spot_validation"] == "normal" and cfg["bands"] == ["20m"]


def test_a_running_receiver_is_not_removed(radio_seat: Seat) -> None:
    radio_seat.add("Dallas-1", "40m")
    radio_seat.start("Dallas-1")
    with pytest.raises(SeatError, match="running"):
        radio_seat.remove("Dallas-1")
    assert any(i.name == "Dallas-1" for i in radio_seat.instances())


def test_only_a_flex_receiver_of_a_radio_file_is_removed(radio_seat: Seat) -> None:
    with pytest.raises(SeatError, match="only a Flex receiver"):
        radio_seat.remove("dallas1")  # set up by hand, no radio file
    assert any(i.name == "dallas1" for i in radio_seat.instances())


def test_add_counts_a_receiver_set_up_by_address_and_default_ports(radio_seat: Seat) -> None:
    path = radio_seat.root / "config" / "Dallas.radio.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "address": "192.0.2.9",
                                "max_panadapters": 4, "max_slices": 4, "daxiq_channels": 4}))
    write_config(radio_seat.instance("dallas1").config, {"flex_daxiq_channel": 1})  # sdr_ip 192.0.2.9
    with pytest.raises(SeatError, match="DAX-IQ 1 is already used by dallas1"):
        radio_seat.add("Dallas-1", "40m")
    radio_seat.add("Dallas-2", "40m")  # dallas1 and dallas2 run on the defaults 7300 and 7791
    cfg = _cfg(radio_seat, "Dallas-2")
    assert (cfg["telnet_port"], cfg["flex_udp_port"]) == (7332, 7792)


def test_add_skips_a_port_another_program_holds(radio_seat: Seat, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seat_mod, "_port_free", lambda port, **_: port != 7331)  # Aggregator-style squatter
    radio_seat.add("Dallas-1", "40m")
    assert _cfg(radio_seat, "Dallas-1")["telnet_port"] == 7332


def test_port_free_sees_a_listening_port() -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        assert not seat_mod._port_free(busy.getsockname()[1], host="127.0.0.1")


def test_add_leaves_the_templates_antenna_and_gain_out(radio_seat: Seat) -> None:
    path = radio_seat.root / "code" / "skimmer_flex.example.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "flex_rxant": "ANT2", "flex_rfgain": 16}))
    radio_seat.add("Dallas-1", "40m")
    assert not {"flex_rxant", "flex_rfgain"} & set(_cfg(radio_seat, "Dallas-1"))


def test_add_refuses_an_earlier_config_of_another_channel(radio_seat: Seat) -> None:
    conf = radio_seat.root / "config" / "Dallas-1.local.json"
    conf.write_text(json.dumps({"sdr_type": "flex", "radio": "Dallas", "flex_daxiq_channel": 2}))
    with pytest.raises(SeatError, match="is for Dallas DAX-IQ 2"):
        radio_seat.add("Dallas-1", "40m")


def test_a_radio_name_with_a_hyphen_takes_receivers(radio_seat: Seat) -> None:
    src = radio_seat.root / "config" / "Dallas.radio.json"
    (radio_seat.root / "config" / "FLEX-6400M-1234.radio.json").write_text(src.read_text())
    assert radio_seat.add("FLEX-6400M-1234-1", "40m").startswith("FLEX-6400M-1234-1: added on DAX-IQ 1")


def test_add_ignores_a_machines_template_for_the_shipped_defaults(radio_seat: Seat) -> None:
    (radio_seat.root / "config" / "template.json").write_text(json.dumps({"spot_validation": "off"}))
    radio_seat.add("Dallas-1", "40m")
    assert "spot_validation" not in _cfg(radio_seat, "Dallas-1")
    assert radio_seat.default_settings()["validation"] == "off"  # left unset: off


def test_a_removed_receivers_settings_are_offered_back(radio_seat: Seat) -> None:
    radio_seat.add("Dallas-1", "40m")
    write_config(radio_seat.instance("Dallas-1").config, {"itila2_afc": True})
    assert radio_seat.removed_settings({"Dallas-1"}) == {}
    radio_seat.remove("Dallas-1")
    back = radio_seat.snapshot()["removed"]
    assert list(back) == ["Dallas-1"] and back["Dallas-1"]["afc"] is True
