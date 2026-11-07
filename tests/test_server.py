"""sparkgap_server.py: the seat over a WebSocket, a copy's feed passed through, the web page."""

import json
import socket
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import sparkgap_server
from feed_server import OP_CLOSE, OP_TEXT, FeedServer
from seat import Seat
from sparkgap_server import SERVER_PROTOCOL, Server, load_settings
from tests.test_feed_server import WS
from tests.test_seat import seat, with_radio  # noqa: F401  (the fixture: a temporary root with stand-ins)

CODE = "ACDEFH"


@pytest.fixture
def server(seat: Seat) -> Iterator[Server]:  # noqa: F811
    a = Server(seat.root, {"bind": "127.0.0.1", "port": 0, "join_code": CODE})
    threading.Thread(target=lambda: _run(a), daemon=True).start()
    assert a.ready.wait(5)
    yield a


def _run(a: Server) -> None:
    import asyncio
    import contextlib

    with contextlib.suppress(RuntimeError, asyncio.CancelledError):  # a test closed it
        asyncio.run(a.serve())


def _ws(a: Server, code: str = CODE, path: str = "/server", proto: str = SERVER_PROTOCOL) -> WS:
    return WS(a.port, path=path, protocols=f"{proto}, sparkgap.code.{code}")


def _result(ws: WS, rid: int) -> dict:
    while True:
        msg = ws.json("server_result")
        if msg["id"] == rid:
            return msg


def test_a_wrong_code_is_refused(server: Server) -> None:
    ws = _ws(server, code="XXXXXX")
    op, data = ws.frame()
    assert op == OP_CLOSE and int.from_bytes(data[:2], "big") == 4401


def test_hello_then_the_seat_without_copy_codes(server: Server) -> None:
    ws = _ws(server)
    hello = ws.json("hello")
    assert hello["server"] is True and ws.headers.count(SERVER_PROTOCOL) == 1
    st = ws.json("seat")
    assert [i["name"] for i in st["instances"]] == ["dallas1", "dallas2"]
    assert all("feed_code" not in i for i in st["instances"])


def test_start_through_the_server_and_busy_while_it_runs(server: Server) -> None:
    ws = _ws(server)
    msg = {"type": "server", "id": 1, "cmd": "start", "name": "dallas1"}
    ws.send(OP_TEXT, json.dumps(msg).encode())
    ws.send(OP_TEXT, json.dumps({**msg, "id": 2}).encode())
    assert "busy" in _result(ws, 2)["error"]
    assert _result(ws, 1)["ok"] and server.seat.pid("dallas1")


def test_a_setting_off_the_list_is_refused(server: Server) -> None:
    ws = _ws(server)
    msg = {
        "type": "server",
        "id": 3,
        "cmd": "set",
        "name": "dallas1",
        "settings": {"callsign": "X"},
    }
    ws.send(OP_TEXT, json.dumps(msg).encode())
    r = _result(ws, 3)
    assert not r["ok"] and "cannot be changed" in r["error"]


def test_a_copys_feed_is_passed_through_with_its_own_code(server: Server, seat: Seat) -> None:  # noqa: F811
    feed = FeedServer({"bind": "127.0.0.1", "port": 0, "join_code": "ABCDEF"}, {"bands": []})
    feed.start()
    try:
        copy = seat.root / "code" / "sparkgap.py"
        copy.write_text(copy.read_text().replace("7321", str(feed.port)))
        seat.start("dallas1")
        ws = _ws(server, path="/feed/dallas1", proto="sparkgap.v1")
        assert "101" in ws.headers.splitlines()[0]
        assert ws.json("hello")["v"] == 1 and ws.json("snapshot")
    finally:
        feed.stop()


def test_a_stopped_copy_has_no_feed(server: Server) -> None:
    ws = _ws(server, path="/feed/dallas2", proto="sparkgap.v1")
    op, data = ws.frame()
    assert op == OP_CLOSE and b"no feed running" in data


def test_sparkgap_web_is_served(server: Server) -> None:
    with socket.create_connection(("127.0.0.1", server.port), timeout=3) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        time.sleep(0.2)
        head = s.recv(200)
    assert head.startswith(b"HTTP/1.1 200") and b"text/html" in head


def test_first_run_writes_an_owner_only_settings_file(tmp_path: Path) -> None:
    cfg = load_settings(tmp_path)
    path = tmp_path / "server.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and len(cfg["join_code"]) == 6
    assert cfg["port"] == sparkgap_server.DEFAULT_PORT == 7320
    assert load_settings(tmp_path) == cfg


def test_a_client_that_vanishes_is_not_an_error(
    server: Server, caplog: pytest.LogCaptureFixture
) -> None:
    ws = _ws(server)
    ws.json("seat")
    ws.s.close()  # no close frame: a closed tab or a dropped link
    time.sleep(0.3)
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")]
    assert not server.clients


def test_half_open_requests_beyond_the_cap_are_closed(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sparkgap_server, "MAX_PENDING", 2)
    idle = [socket.create_connection(("127.0.0.1", server.port)) for _ in range(2)]
    time.sleep(0.2)
    with socket.create_connection(("127.0.0.1", server.port), timeout=2) as s:
        assert s.recv(1) == b""  # closed before any request is read
    for s in idle:
        s.close()


def test_the_copy_side_is_closed_when_the_browser_leaves(server: Server) -> None:
    """A copy that never sends: the server must still let go of it once the browser has gone."""
    srv = socket.create_server(("127.0.0.1", 0))
    upstream_closed = threading.Event()

    def copy() -> None:
        conn, _ = srv.accept()
        raw = b""
        while b"\r\n\r\n" not in raw:
            raw += conn.recv(1024)
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        )
        conn.settimeout(5)
        try:
            if conn.recv(1024) == b"":
                upstream_closed.set()
        except OSError:
            pass

    threading.Thread(target=copy, daemon=True).start()
    server._feed_target = lambda name: ("127.0.0.1", srv.getsockname()[1], None)  # type: ignore[method-assign]
    ws = _ws(server, path="/feed/dallas1", proto="sparkgap.v1")
    assert "101" in ws.headers.splitlines()[0]
    ws.s.close()
    assert upstream_closed.wait(3)
    srv.close()


def test_a_copy_port_search_of_the_server_is_not_a_wrong_code(server: Server) -> None:
    for _ in range(6):  # a web page searching 7320 for copies, with a copy's code
        _ws(server, code="XXXXXX", path="/feed", proto="sparkgap.v1").s.close()
    ws = _ws(server)
    assert ws.json("hello")["server"] is True  # not locked out


def test_the_server_stops_on_sigint_even_when_started_with_it_ignored(seat: Seat) -> None:  # noqa: F811
    """Started in the background over ssh, it inherits SIGINT ignored (Beelink, 2026-10-22)."""
    import os
    import signal
    import subprocess
    import sys

    (seat.root / "server.json").write_text(
        json.dumps({"bind": "127.0.0.1", "port": 0, "join_code": CODE})
    )
    p = subprocess.Popen(
        [sys.executable, "sparkgap_server.py", "--root", str(seat.root)],
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pidfile = seat.root / "logs" / "server.pid"
    for _ in range(50):
        if pidfile.exists():
            break
        time.sleep(0.1)
    time.sleep(0.5)
    os.kill(p.pid, signal.SIGINT)
    assert p.wait(timeout=5) == 0 and not pidfile.exists()


def test_the_server_log_comes_on_connect_and_line_by_line(server: Server) -> None:
    ws = _ws(server)
    first = ws.json("log")
    assert first["reset"] and any("Server on" in line["msg"] for line in first["lines"])
    ws.send(
        OP_TEXT, json.dumps({"type": "server", "id": 9, "cmd": "stop", "name": "dallas2"}).encode()
    )
    while True:
        msg = ws.json("log")
        if any("stop dallas2 by a client" in line["msg"] for line in msg["lines"]):
            break


def test_help_is_the_guide_shipped_with_the_code(server: Server) -> None:
    def get(path: str) -> bytes:
        with socket.create_connection(("127.0.0.1", server.port), timeout=3) as s:
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            data = b""
            while chunk := s.recv(65536):
                data += chunk
        return data

    assert get("/help").startswith(b"HTTP/1.1 200") and b"SparkGap Web" in get("/help")
    assert get("/help/SERVER_PROTOCOL.md").startswith(b"HTTP/1.1 200")
    assert get("/help/../sparkgap.py").startswith(b"HTTP/1.1 404")


def test_the_server_exits_on_sigterm_with_a_browser_still_connected(seat: Seat) -> None:  # noqa: F811
    """A connected browser kept the process alive after SIGTERM (Dallas, 2026-10-22)."""
    import os
    import signal
    import subprocess
    import sys

    (seat.root / "server.json").write_text(
        json.dumps({"bind": "127.0.0.1", "port": 0, "join_code": CODE})
    )
    p = subprocess.Popen(
        [sys.executable, "sparkgap_server.py", "--root", str(seat.root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    port = 0
    assert p.stdout is not None
    for line in p.stdout:
        if "Server on" in line:
            port = int(line.split("127.0.0.1:")[1].split()[0])
            break
    ws = WS(port, path="/server", protocols=f"{SERVER_PROTOCOL}, sparkgap.code.{CODE}")
    ws.json("hello")
    os.kill(p.pid, signal.SIGTERM)
    assert p.wait(timeout=5) == 0


def _copy_on_feed(seat: Seat, answer: tuple[bool, str] | None) -> FeedServer:  # noqa: F811
    """A stand-in copy whose feed answers a live change with `answer` (None: never answers)."""
    feed = FeedServer({"bind": "127.0.0.1", "port": 0, "join_code": "ABCDEF"}, {"bands": []})
    if answer is not None:
        feed.on_control = lambda change, reply: reply(*answer)
    feed.start()
    copy = seat.root / "code" / "sparkgap.py"
    copy.write_text(copy.read_text().replace("7321", str(feed.port)))
    return feed


def _set_band(server: Server, band: str) -> dict:
    ws = _ws(server)
    ws.json("hello")
    ws.send(OP_TEXT, json.dumps({"type": "server", "id": 1, "cmd": "set", "name": "dallas2",
                                 "settings": {"band": band}}).encode())
    return _result(ws, 1)


def test_a_band_change_moves_a_running_copy_in_place(server: Server, seat: Seat) -> None:  # noqa: F811
    feed = _copy_on_feed(seat, (True, "retuned to 20m"))
    try:
        seat.start("dallas2")
        pid = seat.pid("dallas2")
        r = _set_band(server, "20m")
        assert r["ok"] and r["message"] == "dallas2: retuned to 20m"
        assert seat.pid("dallas2") == pid and seat.band(seat.instance("dallas2")) == "20m"
    finally:
        feed.stop()


def test_a_copy_that_cannot_change_band_live_is_restarted_on_it(server: Server, seat: Seat) -> None:  # noqa: F811
    feed = _copy_on_feed(seat, (False, "not supported while recording"))
    try:
        seat.start("dallas2")
        pid = seat.pid("dallas2")
        r = _set_band(server, "20m")
        assert r["ok"] and "live" in r["message"] and seat.pid("dallas2") not in (None, pid)
        assert seat.band(seat.instance("dallas2")) == "20m"
    finally:
        feed.stop()


def test_a_recovering_copy_is_neither_moved_nor_restarted(server: Server, seat: Seat) -> None:  # noqa: F811
    feed = _copy_on_feed(seat, (False, "recovering from a radio outage: try again once it is back"))
    try:
        seat.start("dallas2")
        pid = seat.pid("dallas2")
        r = _set_band(server, "20m")
        assert not r["ok"] and "recovering" in r["error"] and "nothing changed" in r["error"]
        assert seat.pid("dallas2") == pid and seat.band(seat.instance("dallas2")) == "40m"
    finally:
        feed.stop()


def test_a_browsers_feed_is_opened_view_only() -> None:
    req = sparkgap_server.feed_request
    assert req("127.0.0.1", 7321, "ABCDEF", view=True).startswith(b"GET /feed?view ")
    assert req("127.0.0.1", 7321, "ABCDEF").startswith(b"GET /feed ")
    assert req("127.0.0.1", 7321, "ABCDEF", view=True, browser="100.1.2.3").startswith(
        b"GET /feed?view&for=100.1.2.3 "
    )
    assert req("127.0.0.1", 7321, "ABCDEF", browser="100.1.2.3").startswith(b"GET /feed ")


def test_feed_control_without_a_feed_is_no_answer() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert sparkgap_server.feed_control("127.0.0.1", port, None, {"band": "20m"}, wait_s=1) is None


def test_shutdown_drops_a_browser_that_stopped_reading(server: Server) -> None:
    """A half-closed browser with 63 KB queued kept the old server alive (Beelink, 2026-10-22)."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    s.connect(("127.0.0.1", server.port))
    s.sendall(b"GET /server HTTP/1.1\r\n")  # unfinished, then never reads
    t = time.monotonic()
    while not server.conns:
        assert time.monotonic() - t < 3
        time.sleep(0.01)
    w = next(iter(server.conns))
    assert server.listener is not None
    loop = server.listener.get_loop()
    loop.call_soon_threadsafe(w.write, b"x" * 8 * 2**20)
    time.sleep(0.3)
    assert w.transport.get_write_buffer_size() > 0  # stuck: the browser takes nothing
    loop.call_soon_threadsafe(server.close_connections)
    t = time.monotonic()
    while w.get_extra_info("socket").fileno() != -1:
        assert time.monotonic() - t < 2, "connection still open"
        time.sleep(0.01)
    s.close()


def test_the_server_exits_on_sigterm_while_a_start_is_under_way(seat: Seat) -> None:  # noqa: F811
    """A start waits up to 45 s for its copy to go live; SIGTERM must not wait (2026-10-22)."""
    import os
    import signal
    import subprocess
    import sys

    copy = seat.root / "code" / "sparkgap.py"
    copy.write_text(copy.read_text().replace("SparkGap LIVE:", "still connecting"))
    (seat.root / "server.json").write_text(
        json.dumps({"bind": "127.0.0.1", "port": 0, "join_code": CODE})
    )
    p = subprocess.Popen(
        [sys.executable, "sparkgap_server.py", "--root", str(seat.root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    port = 0
    assert p.stdout is not None
    for line in p.stdout:
        if "Server on" in line:
            port = int(line.split("127.0.0.1:")[1].split()[0])
            break
    ws = WS(port, path="/server", protocols=f"{SERVER_PROTOCOL}, sparkgap.code.{CODE}")
    ws.json("hello")
    start = {"type": "server", "id": 1, "cmd": "start", "name": "dallas1"}
    ws.send(OP_TEXT, json.dumps(start).encode())
    time.sleep(1)
    t = time.monotonic()
    os.kill(p.pid, signal.SIGTERM)
    assert p.wait(timeout=10) == 0 and time.monotonic() - t < 5


def test_a_feed_bound_off_this_machine_is_not_passed_through(server: Server, seat: Seat) -> None:  # noqa: F811
    """The proxy would send the copy's join code over the network (audit 2026-10-22)."""
    from seat import write_config

    write_config(seat.root / "config" / "dallas1.local.json",
                 {"gui_feed": {"bind": "192.0.2.5", "port": "auto", "join_code": "ABCDEF"}})
    seat.start("dallas1")
    assert server._feed_target("dallas1") is None  # pyright: ignore[reportPrivateUsage]


def test_a_network_bind_also_serves_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    own = sparkgap_server.own_addresses
    assert own("127.0.0.1") == own("localhost") == own("0.0.0.0") == own("::") == []

    def resolve(host: str, *_: object) -> list[tuple]:
        assert host == "rednuc"
        addrs = ("127.0.1.1", "127.0.0.1", "100.64.0.1")
        return [(socket.AF_INET, 0, 0, "", (a, 0)) for a in addrs]

    monkeypatch.setattr(sparkgap_server.socket, "getaddrinfo", resolve)
    assert own("100.64.0.1", "rednuc") == ["127.0.0.1", "127.0.1.1"]


def test_an_unknown_own_name_still_serves_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_: object) -> list[tuple]:
        raise socket.gaierror("no name")

    monkeypatch.setattr(sparkgap_server.socket, "getaddrinfo", fail)
    assert sparkgap_server.own_addresses("192.168.0.10", "nowhere") == ["127.0.0.1"]


def test_discover_takes_an_address_and_refuses_anything_else(
        seat: Seat, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    asked: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(seat, "discover",
                        lambda seconds=0, address=None, kind=None, name=None: asked.append((address, kind)) or "ok")
    a = Server(seat.root, {"bind": "127.0.0.1", "port": 0, "join_code": CODE})
    a.seat = seat
    assert a._run_op("discover", "discover", {"address": "192.0.2.101"}) == "ok"
    assert a._run_op("discover", "discover", {}) == "ok"
    assert a._run_op("discover", "discover", {"address": "192.0.2.120", "type": "hpsdr"}) == "ok"
    with pytest.raises(ValueError):
        a._run_op("discover", "discover", {"address": "192.0.2.101; reboot"})
    assert asked == [("192.0.2.101", None), (None, None), ("192.0.2.120", "hpsdr")]


def test_node_writes_what_sparkgap_sh_setup_writes(seat: Seat, tmp_path: Path) -> None:  # noqa: F811
    """The web's setup and sparkgap.sh setup: the same answers, the same radio file."""
    import json as j

    from tools import setup_node
    cfg = seat.root / "config"
    (cfg / "Lake.radio.json").write_text(j.dumps({"nickname": "Lake", "model": "FLEX-6600"}))
    a = Server(seat.root, {"bind": "127.0.0.1", "port": 0, "join_code": CODE})
    a.seat = seat
    answers_web = {"radio": "Lake", "callsign": "n0call-2", "grid": "em12ub", "op_name": "Op"}
    out = a._run_op("node", "node", answers_web)
    assert "Lake.radio.json: N0CALL-2 EM12ub Op" in out
    by_web = j.loads((cfg / "Lake.radio.json").read_text())
    (cfg / "Lake.radio.json").write_text(j.dumps({"nickname": "Lake", "model": "FLEX-6600"}))
    answers = iter(["n0call-2", "em12ub", "Op", "y"])
    setup_node.setup(seat.root, "Lake", lambda _p: next(answers))
    assert j.loads((cfg / "Lake.radio.json").read_text()) == by_web
    with pytest.raises(ValueError, match="a callsign"):
        a._run_op("node", "node", {**answers_web, "callsign": "bad call"})


@pytest.mark.parametrize("by, want", [
    ("SparkGap Web", "SparkGap Web (192.0.2.7)"), ("sparkgap.sh", "sparkgap.sh (192.0.2.7)"),
    (None, "a client (192.0.2.7)"), ("<b>x</b>\nfake line", "bxbfake line (192.0.2.7)"),
])
def test_the_log_names_who_asked(by: object, want: str) -> None:
    assert sparkgap_server.requester(by, "192.0.2.7") == want


def test_a_request_in_words() -> None:
    assert sparkgap_server.what_asked("stop", "Dallas-2", {}) == "stop Dallas-2"
    assert sparkgap_server.what_asked("set", "Dallas-2", {"rf_gain": 0}) == "set Dallas-2 rf_gain 0"
    assert sparkgap_server.what_asked("discover", "discover", {}) == "discover"


def test_a_refused_request_is_logged(server: Server) -> None:
    """The web's footer shows the server log, so a refusal must reach it."""
    ws = _ws(server)
    ws.json("log")
    ws.send(OP_TEXT, json.dumps({"type": "server", "id": 3, "cmd": "explode", "name": "dallas2",
                                 "by": "SparkGap Web"}).encode())
    while True:
        msg = ws.json("log")
        if any("explode dallas2 refused: unknown command" in line["msg"] for line in msg["lines"]):
            break


def test_add_then_remove_through_the_server(server: Server, seat: Seat, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    with_radio(seat, monkeypatch)
    ws = _ws(server)
    ws.send(OP_TEXT, json.dumps({"type": "server", "id": 7, "cmd": "add", "name": "Dallas-1",
                                 "settings": {"band": "40m"}}).encode())
    r = _result(ws, 7)
    assert r["ok"] and r["message"].startswith("Dallas-1: added on DAX-IQ 1, 40m"), r
    assert seat.pid("Dallas-1")
    ws.send(OP_TEXT, json.dumps({"type": "server", "id": 8, "cmd": "remove", "name": "Dallas-1"}).encode())
    r = _result(ws, 8)
    assert r["ok"] and "removed" in r["message"], r
    assert not seat.pid("Dallas-1") and all(i.name != "Dallas-1" for i in seat.instances())


def test_add_writes_its_settings_and_a_refused_one_adds_nothing(
    server: Server, seat: Seat, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    with_radio(seat, monkeypatch)
    ws = _ws(server)
    msg = {"type": "server", "id": 9, "cmd": "add", "name": "Dallas-2",
           "settings": {"band": "20m", "validation": "off", "afc": True}}
    ws.send(OP_TEXT, json.dumps(msg).encode())
    assert _result(ws, 9)["ok"]
    cfg = json.loads((seat.root / "config" / "Dallas-2.local.json").read_text())
    assert cfg["spot_validation"] == "off" and cfg["itila2_afc"] is True
    msg = {"type": "server", "id": 10, "cmd": "add", "name": "Dallas-1",
           "settings": {"band": "40m", "validation": "loose"}}
    ws.send(OP_TEXT, json.dumps(msg).encode())
    r = _result(ws, 10)
    assert not r["ok"] and "validation must be" in r["error"]
    assert all(i.name != "Dallas-1" for i in seat.instances()) and not seat.pid("Dallas-1")


def test_an_hpsdr_receiver_is_added_to_its_radio_s_process_and_a_radio_renamed(
    server: Server, seat: Seat, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from tests.test_seat import with_hpsdr

    with_hpsdr(seat, monkeypatch)
    ws = _ws(server)
    ws.send(OP_TEXT, json.dumps({"type": "server", "id": 11, "cmd": "add", "name": "Web888-1",
                                 "settings": {"band": "20m", "validation": "off"}}).encode())
    r = _result(ws, 11)
    assert r["ok"] and "Web888: live" in r["message"]
    assert json.loads((seat.root / "config" / "Web888.local.json").read_text())["spot_validation"] == "off"
    ws.send(OP_TEXT, json.dumps({"type": "server", "id": 12, "cmd": "rename", "name": "rename",
                                 "settings": {"radio": "Web888", "name": "Pitaya"}}).encode())
    r = _result(ws, 12)
    assert not r["ok"] and "stop Web888 first" in r["error"]
    pitaya = {"type": "hpsdr", "address": "192.0.2.121", "receivers": 8, "callsign": "N0CALL-7", "grid": "EM12ub"}
    (seat.root / "config" / "Pitaya.radio.json").write_text(json.dumps(pitaya))
    ws.send(OP_TEXT, json.dumps({"type": "server", "id": 13, "cmd": "add", "name": "Pitaya-1",
                                 "settings": {"band": "40m", "validation": "loose"}}).encode())
    r = _result(ws, 13)
    assert not r["ok"] and "validation must be" in r["error"]
    assert all(i.name != "Pitaya" for i in seat.instances())  # its process went with the refused add
    ws.send(OP_TEXT, json.dumps({"type": "server", "id": 16, "cmd": "stop", "name": "Web888-1"}).encode())
    assert _result(ws, 16)["ok"] and not seat.pid("Web888")  # a receiver's name stops its radio's process
    ws.send(OP_TEXT, json.dumps({"type": "server", "id": 14, "cmd": "remove", "name": "Web888-1"}).encode())
    r = _result(ws, 14)  # its last receiver: the feeder and process stop, then it is removed
    assert r["ok"] and "Web888's last receiver" in r["message"]
    assert all(i.name != "Web888" for i in seat.instances()) and not seat.pid("Web888")
    ws.send(OP_TEXT, json.dumps({"type": "server", "id": 15, "cmd": "remove_radio", "name": "remove_radio",
                                 "settings": {"radio": "Web888"}}).encode())
    assert _result(ws, 15)["ok"] and not (seat.root / "config" / "Web888.radio.json").exists()


def test_a_station_and_its_warnings_reach_the_server(server: Server) -> None:
    # Dallas 11-05: the station's health carries type flex-station, which hid every status message,
    # and its warnings showed only in the receiver's problems, never in the server log
    import logging

    from process_link import ProcessLink

    ws = _ws(server)
    ws.json("log")
    link = ProcessLink("station", "Lake", port=server.port).start()
    try:
        assert link.connected.wait(5)
        link.status(type="flex-station", state="reconnecting")
        logging.getLogger("sparkgap.flex").warning("[Flex] Radio 192.0.2.1 unreachable for 3 s")
        want = {"Server: radio Lake reconnecting", "Station Lake: [Flex] Radio 192.0.2.1 unreachable for 3 s"}
        seen: set[str] = set()
        while not want <= seen:
            seen |= {line["msg"] for line in ws.json("log")["lines"]}
        assert server.procs[("station", "Lake")].status["state"] == "reconnecting"
    finally:
        link.close()


@pytest.mark.parametrize("cmd, settings, want", [
    ("node", {"callsign": "WX7V-5"}, "saving the node"), ("stop", {}, "stopping"), ("start", {}, "starting"),
    ("set", {"band": "20m"}, "changing band"), ("set", {"rbn_upload": "dry"}, "changing its rbn feeder"),
    ("set", {"record": True}, "restarting"), ("discover", {}, "discovering"),
])
def test_a_request_shows_its_own_word_while_it_runs(cmd: str, settings: dict, want: str) -> None:
    assert sparkgap_server.busy_word(cmd, settings) == want
