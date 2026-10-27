"""seat.py runs a machine's SparkGap instances: real processes from
stand-in sparkgap.py and flex_station.py scripts in a temporary root."""

import json
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
signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
while True:
    time.sleep(0.1)
"""
STATION = """import json, signal, sys, time
status = sys.argv[sys.argv.index("--status") + 1]
print("12:00:00 INFO [Flex] Station SparkGap-Test holds the GUI client (client 0x1)", flush=True)
json.dump({"station": "SparkGap-Test", "state": "live", "rtt_ms": 2.0,
           "losses": [[time.time() - 60, "No ping reply for 23 s"]]}, open(status, "w"))
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
    st = snap["stations"][0]
    assert st["live"] == "live" and st["rtt_ms"] == 2.0 and st["losses_hour"] == 1


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
        ({"spot_rule_split_runs": "yes"}, "true or false"),
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
    assert path.read_text() == '{\n    "b": 2,\n    "c": [\n        1\n    ]\n}\n'


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
