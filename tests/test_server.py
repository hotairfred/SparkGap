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
from tests.test_seat import seat  # noqa: F401  (the fixture: a temporary root with stand-ins)

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
        if any("asks stop dallas2" in line["msg"] for line in msg["lines"]):
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
