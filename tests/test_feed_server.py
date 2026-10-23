"""GUI feed: WebSocket handshake, framing (RFC 6455), join code, snapshot, publishing, spectrum."""

from __future__ import annotations

import base64
import collections
import json
import logging
import os
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import feed_server
from feed_server import (
    CLOSE_LOCKED_OUT,
    CLOSE_WRONG_CODE,
    CODE_LENGTH,
    CODE_MINT_ALPHABET,
    CODE_PROTOCOL,
    FEED_VERSION,
    LOCKOUT_S,
    LOCKOUT_TRIES,
    OP_BINARY,
    OP_CLOSE,
    OP_CONT,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    PROTOCOL,
    SPECTRUM_HEADER,
    SPECTRUM_ROWS_HZ,
    FeedServer,
    SpectrumTap,
    accept_key,
    encode_frame,
    new_code,
    normalize_code,
    show_code,
    unmask,
)

HELLO = {"call": "W1AW", "band": "40m", "center_khz": 7090.0, "span_khz": 96.0, "decoder": "itila2"}


def test_accept_key_rfc_example() -> None:
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize("n", [0, 5, 125, 126, 65_535, 65_536])
def test_encode_frame_lengths(n: int) -> None:
    f = encode_frame(OP_BINARY, b"x" * n)
    assert f[0] == 0x80 | OP_BINARY
    if n < 126:
        assert f[1] == n and len(f) == 2 + n
    elif n < 65_536:
        assert f[1] == 126 and struct.unpack("!H", f[2:4])[0] == n
    else:
        assert f[1] == 127 and struct.unpack("!Q", f[2:10])[0] == n


def test_unmask_rfc_example() -> None:
    assert (
        unmask(bytes([0x7F, 0x9F, 0x4D, 0x51, 0x58]), bytes([0x37, 0xFA, 0x21, 0x3D])) == b"Hello"
    )


def test_non_loopback_needs_join_code() -> None:
    with pytest.raises(ValueError):
        FeedServer({"bind": "100.64.0.1"}, HELLO)
    with pytest.raises(ValueError):
        FeedServer({"bind": "100.64.0.1", "join_code": "XXXXX"}, HELLO)  # too short
    with pytest.raises(ValueError):
        FeedServer({"bind": "100.64.0.1", "join_code": "XXXXXO"}, HELLO)  # O is not in the alphabet
    assert FeedServer({"bind": "100.64.0.1", "join_code": "xxx-xxx"}, HELLO).code == "XXXXXX"


@pytest.mark.parametrize("cfg", [True, "127.0.0.1", {"port": None}, {"port": "x"}, {"port": -1},
                                 {"port": 65_536}, {"port": 1.5}, {"port": True}])
def test_bad_block_is_a_value_error(cfg) -> None:
    # startup logs a ValueError and keeps running; anything else would stop SparkGap
    with pytest.raises(ValueError):
        FeedServer(cfg, HELLO)


def test_new_code_from_mint_alphabet() -> None:
    codes = {new_code() for _ in range(200)}
    assert len(codes) > 190
    assert all(len(c) == CODE_LENGTH and set(c) <= set(CODE_MINT_ALPHABET) for c in codes)
    assert show_code("xxxxxx") == "XXX-XXX" and normalize_code(" xxx x-xx") == "XXXXXX"


class WS:
    """Minimal test client."""

    def __init__(
        self,
        port: int,
        token: str | None = None,
        path: str = "/feed",
        extra: list[str] | None = None,
        protocols: str = PROTOCOL,
    ) -> None:
        self.s = socket.create_connection(("127.0.0.1", port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        hdr = [
            f"GET {path} HTTP/1.1",
            "Host: x",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            f"Sec-WebSocket-Protocol: {protocols}",
            *(extra or []),
        ]
        if token:
            hdr.append(f"Authorization: Bearer {token}")
        self.s.sendall(("\r\n".join(hdr) + "\r\n\r\n").encode())
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.s.recv(1)
            if not chunk:
                break
            raw += chunk
        self.status = int(raw.split(b" ")[1]) if raw else 0
        self.headers = raw.decode()
        self.key = key

    def recv_exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.s.recv(n - len(out))
            assert chunk, "connection closed"
            out += chunk
        return out

    def frame(self) -> tuple[int, bytes]:
        b0, b1 = self.recv_exact(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self.recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self.recv_exact(8))[0]
        return b0 & 0x0F, self.recv_exact(n)

    def json(self, kind: str) -> dict:
        while True:
            op, data = self.frame()
            if op == OP_TEXT:
                msg = json.loads(data)
                if msg["type"] == kind:
                    return msg

    def send(self, op: int, data: bytes, fin: bool = True) -> None:
        mask = os.urandom(4)
        assert len(data) < 126
        self.s.sendall(bytes([0x80 * fin | op, 0x80 | len(data)]) + mask + unmask(data, mask))


@pytest.fixture
def feed():
    f = FeedServer({"bind": "127.0.0.1", "port": 0}, HELLO)
    f.start()
    yield f
    f.stop()


def wait_for(cond, timeout: float = 3.0) -> None:
    t = time.monotonic()
    while not cond():
        assert time.monotonic() - t < timeout
        time.sleep(0.01)


def test_handshake_hello_snapshot(feed: FeedServer) -> None:
    feed.publish_window(7021.2, "CQ DE AF4PX AF4PX K", 22, 18.0, 0.4)
    wait_for(lambda: len(feed.windows) == 1)
    c = WS(feed.port)
    assert c.status == 101
    assert f"Sec-WebSocket-Accept: {accept_key(c.key)}" in c.headers
    assert f"Sec-WebSocket-Protocol: {PROTOCOL}" in c.headers
    hello = c.json("hello")
    assert hello["v"] == FEED_VERSION and hello["band"] == "40m"
    snap = c.json("snapshot")
    assert snap["windows"][0]["text"] == "CQ DE AF4PX AF4PX K"
    assert snap["signals"][0]["khz"] == 7021.2


def test_live_messages(feed: FeedServer) -> None:
    c = WS(feed.port)
    c.json("snapshot")
    wait_for(lambda: feed.n_clients == 1)
    feed.publish_spot(7021.2, "AF4PX", 18, 22)
    assert c.json("spot")["call"] == "AF4PX"
    feed.publish_status({"bins": 37, "cpu": 38})
    assert c.json("status")["bins"] == 37
    # numpy values, as a decoder may hand them, still serialise
    khz: Any = np.float32(7021.25)
    snr: Any = np.float32(18.5)
    wpm: Any = np.int64(22)
    feed.publish_spot(khz, "AF4PX", snr, wpm)
    assert c.json("spot")["snr"] == 18.5
    cost: Any = np.float32(0.5)
    feed.publish_window(khz, "CQ", wpm, snr, cost)
    assert c.json("window")["cost"] == 0.5


@pytest.mark.parametrize("found", [True, False])
def test_console_name(feed: FeedServer, monkeypatch: pytest.MonkeyPatch, found: bool) -> None:
    def lookup(host: str) -> tuple[str, list[str], list[str]]:
        if not found:
            raise socket.herror(1, "Unknown host")
        return "shack.example.net", [], [host]

    monkeypatch.setattr(feed_server.socket, "gethostbyaddr", lookup)
    c = WS(feed.port)
    c.json("snapshot")
    wait_for(lambda: feed.n_clients == 1)
    if found:
        wait_for(lambda: next(iter(feed.clients)).name is not None)
    else:
        time.sleep(0.1)
    feed.publish_status({"bins": 37})
    (client,) = c.json("status")["clients"]
    assert client["name"] == ("shack" if found else None)
    assert client["peer"].startswith("127.0.0.1:")


def test_ping_pong_and_close(feed: FeedServer) -> None:
    c = WS(feed.port)
    c.json("snapshot")
    c.send(OP_PING, b"hi")
    while True:
        op, data = c.frame()
        if op == OP_PONG:
            assert data == b"hi"
            break
    c.send(OP_CLOSE, struct.pack("!H", 1000))
    while True:
        op, data = c.frame()
        if op == OP_CLOSE:
            break
    wait_for(lambda: feed.n_clients == 0)


def test_bad_path_and_unmasked_frame(feed: FeedServer) -> None:
    assert WS(feed.port, path="/nope").status == 404
    c = WS(feed.port)
    c.json("snapshot")
    c.s.sendall(bytes([0x80 | OP_TEXT, 2]) + b"{}")  # unmasked: protocol error
    while True:
        op, data = c.frame()
        if op == OP_CLOSE:
            assert struct.unpack("!H", data[:2])[0] == 1002
            break


def test_join_code_required_when_set() -> None:
    f = FeedServer({"bind": "127.0.0.1", "port": 0, "join_code": "XXXXXX"}, HELLO)
    f.start()
    try:
        assert WS(f.port).status == 401
        assert WS(f.port, token="XXXXXY").status == 401
        assert WS(f.port, token="xxx-xxx").status == 101  # case and dash ignored
    finally:
        f.stop()


def test_lockout_after_wrong_codes() -> None:
    f = FeedServer({"bind": "127.0.0.1", "port": 0, "join_code": "XXXXXX"}, HELLO)
    f.start()
    try:
        wrong = [WS(f.port, token="YYYYYY").status for _ in range(LOCKOUT_TRIES)]
        assert wrong == [401] * LOCKOUT_TRIES
        assert WS(f.port, token="XXXXXX").status == 429  # even the right code, until they age out
        f.misses["127.0.0.1"] = collections.deque(t - LOCKOUT_S - 1 for t in f.misses["127.0.0.1"])
        assert WS(f.port, token="XXXXXX").status == 101
    finally:
        f.stop()


def test_spectrum_tap_peak_and_rate(feed: FeedServer) -> None:
    c = WS(feed.port)
    c.json("snapshot")
    c.send(OP_TEXT, json.dumps({"type": "subscribe", "spectrum_hz": 60}).encode())
    wait_for(lambda: feed.n_clients == 1)
    sr, center = 192_000, 7_090_000.0
    tap = SpectrumTap(feed, sr, center)
    t = np.arange(sr // 4) / sr
    tone = 7_101_850.0 - center  # WA8VTD in Fred's file
    rng = np.random.default_rng(1)
    x = np.exp(2j * np.pi * tone * t) * 50 + (
        rng.normal(size=t.size) + 1j * rng.normal(size=t.size)
    )
    tap.feed_iq(x.real, x.imag)
    while True:
        op, data = c.frame()
        if op == OP_BINARY:
            break
    v, _, first, hz_per_bin, db_unit, n = SPECTRUM_HEADER.unpack_from(data)
    bins = np.frombuffer(data, dtype=np.uint8, offset=SPECTRUM_HEADER.size)
    assert v == FEED_VERSION and n == len(bins) <= 2_048
    peak_hz = first + (int(np.argmax(bins)) + 0.5) * hz_per_bin
    assert abs(peak_hz - 7_101_850.0) < 2 * hz_per_bin
    assert bins.max() * db_unit > 25


def test_tap_floor_follows_edge_rolloff() -> None:
    # IQ whose noise is 25 dB down at both edges, like the radio's filter: edges must not draw black
    f = FeedServer({"bind": "127.0.0.1", "port": 0}, HELLO)
    tap = SpectrumTap(f, 96_000, 7_048_000.0)
    rows: list[np.ndarray] = []
    def grab(first: float, hz: float, unit: float, bins: bytes, utc: float) -> None:
        rows.append(np.frombuffer(bins, np.uint8))

    f.publish_spectrum = grab  # type: ignore[method-assign]
    rng = np.random.default_rng(2)
    gain = np.ones(tap.nfft)
    edge = tap.nfft // 12
    gain[:edge] = gain[-edge:] = 10 ** (-25 / 20)
    for _ in range(20):
        noise = rng.normal(size=tap.nfft) + 1j * rng.normal(size=tap.nfft)
        shaped = np.fft.ifft(np.fft.fft(noise) * np.fft.ifftshift(gain))
        tap.row(shaped / tap.window.clip(0.05), 0.0)
    bins = rows[-1]
    n_edge = len(bins) // 12
    centre_zero = np.mean(bins[len(bins) // 3 : 2 * len(bins) // 3] == 0)
    for part in (bins[n_edge // 4 : n_edge - 16], bins[-n_edge + 16 : -n_edge // 4]):
        assert abs(np.mean(part == 0) - centre_zero) < 0.2



def test_tap_sends_empty_rows_while_the_radio_is_keyed() -> None:
    # a keyed Flex: the IQ carries the transmitter; those rows go out empty and the floor holds
    f = FeedServer({"bind": "127.0.0.1", "port": 0}, HELLO)
    tap = SpectrumTap(f, 96_000, 7_048_000.0)
    rows: list[np.ndarray] = []

    def grab(first: float, hz: float, unit: float, bins: bytes, utc: float) -> None:
        rows.append(np.frombuffer(bins, np.uint8))

    f.publish_spectrum = grab  # type: ignore[method-assign]
    rng = np.random.default_rng(3)
    noise = lambda k: rng.normal(size=tap.nfft) * k + 1j * rng.normal(size=tap.nfft) * k  # noqa: E731
    tap.tx = lambda utc: 10.0 <= utc < 20.0
    for _ in range(20):
        tap.row(noise(1), 0.0)
    floor = None if tap.floor is None else tap.floor.copy()
    tap.row(noise(1000), 15.0)                        # the transmitter, 60 dB up
    assert rows[-1].max() == 0 and len(rows[-1]) == len(rows[0])
    assert floor is not None and tap.floor is not None and np.array_equal(tap.floor, floor)
    tap.row(noise(1), 25.0)
    assert 0 < np.mean(rows[-1] == 0) < 1               # after key-up: the band as before

def test_tap_idle_without_clients() -> None:
    f = FeedServer({"bind": "127.0.0.1", "port": 0}, HELLO)
    tap = SpectrumTap(f, 96_000, 7_090_000.0)
    tap.row = lambda seg, utc: pytest.fail("row computed with no client")  # type: ignore[method-assign]
    tap.feed_iq(np.zeros(96_000), np.zeros(96_000))


def test_tap_rows_per_block() -> None:
    f = FeedServer({"bind": "127.0.0.1", "port": 0}, HELLO)
    f.n_clients = 1
    tap = SpectrumTap(f, 192_000, 7_090_000.0)
    rows: list[int] = []
    tap.row = lambda seg, utc: rows.append(len(seg))  # type: ignore[method-assign]
    for _ in range(10):                               # 10 blocks of 0.1 s = 1 s
        tap.feed_iq(np.zeros(19_200), np.zeros(19_200))
    assert len(rows) == int(SPECTRUM_ROWS_HZ) and set(rows) == {tap.nfft}


def test_thinning_by_count() -> None:
    """A 20 rows/s client gets its share of rows even when a block's rows arrive together."""
    f = FeedServer({"bind": "127.0.0.1", "port": 0}, HELLO)

    class Fake:
        spectrum_hz = 20.0
        spectrum_credit = 0.0
        spectrum: collections.deque = collections.deque(maxlen=1_000)
        dropped_rows = 0

        class ready:
            @staticmethod
            def set() -> None:
                pass

    c = Fake()
    f.clients = {c}  # type: ignore[assignment]
    for _ in range(300):
        f._on_spectrum(b"row")
    assert len(c.spectrum) == round(300 * 20 / SPECTRUM_ROWS_HZ)


def test_stalled_client_is_cut_off(feed: FeedServer, monkeypatch: pytest.MonkeyPatch) -> None:
    # a client that stops reading must not hold its connection (and sender) forever
    monkeypatch.setattr(feed_server, "CLIENT_QUEUE_MAX", 50)
    c = WS(feed.port)
    c.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4_096)
    wait_for(lambda: feed.n_clients == 1)
    for _ in range(400):
        feed.publish_window(7_030.0, "X" * 60_000, 20, 10.0, 1.0)
    wait_for(lambda: feed.n_clients == 0, timeout=10.0)


def test_client_cap(feed: FeedServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_server, "MAX_CLIENTS", 2)
    keep = [WS(feed.port), WS(feed.port)]
    wait_for(lambda: feed.n_clients == 2)
    assert [k.status for k in keep] == [101, 101] and WS(feed.port).status == 503


def test_misses_kept_only_for_failures() -> None:
    f = FeedServer({"bind": "127.0.0.1", "port": 0, "join_code": "XXXXXX"}, HELLO)
    assert f._code_ok("100.64.0.2", "XXXXXX") is True and f.misses == {}
    f.misses["100.64.0.3"] = collections.deque([time.monotonic() - LOCKOUT_S - 1])
    assert f._code_ok("100.64.0.4", "YYYYYY") is False
    assert set(f.misses) == {"100.64.0.4"}  # the stale address is pruned


def test_problems_count_errors_only() -> None:
    log = logging.getLogger("sparkgap.test_problems")
    problems = feed_server.ProblemLog()
    log.addHandler(problems)
    try:
        log.warning("[Flex] <<< R1|10000002|unknown client program")
        log.error("[Flex] The radio closed the control connection")
    finally:
        log.removeHandler(problems)
    s = problems.summary()
    assert s["errors"] == 1 and "warnings" not in s
    assert [e["level"] for e in s["last"]] == ["ERROR"]


@pytest.mark.parametrize("frames", [
    [(OP_CONT, b"x", True)],                       # continuation with nothing to continue
    [(OP_TEXT, b"{", False), (OP_TEXT, b"}", True)],  # new message inside a fragmented one
])
def test_bad_fragment_sequence_closes(feed: FeedServer, frames: list) -> None:
    c = WS(feed.port)
    for op, data, fin in frames:
        c.send(op, data, fin)
    while True:
        op, data = c.frame()
        if op == OP_CLOSE:
            assert struct.unpack("!H", data[:2])[0] == 1002
            return


def test_fragmented_message_accepted(feed: FeedServer) -> None:
    c = WS(feed.port)
    wait_for(lambda: feed.n_clients == 1)
    c.send(OP_TEXT, b'{"type":"subscribe",', fin=False)
    c.send(OP_PING, b"")  # control frames may come between fragments
    c.send(OP_CONT, b'"spectrum_hz":5}')
    wait_for(lambda: next(iter(feed.clients)).spectrum_hz == 5.0)


def http_get(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    raw = b""
    while chunk := s.recv(65_536):
        raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    hdrs = {k.lower(): v.strip() for k, v in (h.split(":", 1) for h in lines[1:])}
    return int(lines[0].split(" ")[1]), hdrs, body


def test_web_pages(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "index.html").write_text("<!doctype html>")
    (tmp_path / "app.js").write_text("1")
    (tmp_path / ".hidden.js").write_text("1")
    for d, leaf in (("fonts", "Sora.ttf"), ("fonts", "OFL.txt"), ("icons", "sparkgap.svg")):
        (tmp_path / d).mkdir(exist_ok=True)
        (tmp_path / d / leaf).write_text("1")
    monkeypatch.setattr(feed_server, "WEB_DIR", tmp_path)
    f = FeedServer({"bind": "127.0.0.1", "port": 0, "web": True}, HELLO)
    f.start()
    try:
        status, hdrs, body = http_get(f.port, "/")
        assert status == 200 and body == b"<!doctype html>" and "text/html" in hdrs["content-type"]
        assert http_get(f.port, "/app.js")[1]["content-type"].startswith("text/javascript")
        assert http_get(f.port, "/fonts/Sora.ttf")[1]["content-type"] == "font/ttf"
        assert http_get(f.port, "/icons/sparkgap.svg")[1]["content-type"] == "image/svg+xml"
        for bad in ("/../feed_server.py", "/fonts/../../feed_server.py", "/.hidden.js",
                    "/fonts/OFL.txt", "/nope.html", "/a/b.js"):
            assert http_get(f.port, bad)[0] == 404, bad
        assert WS(f.port).status == 101  # the feed still answers on /feed
        (tmp_path / "leak.html").symlink_to(Path(feed_server.__file__))
        assert http_get(f.port, "/leak.html")[0] == 404  # a symlink out of web/
    finally:
        f.stop()


def test_web_off_by_default(feed: FeedServer) -> None:
    assert http_get(feed.port, "/")[0] == 404


def close_code(c: WS) -> int:
    while True:
        op, data = c.frame()
        if op == OP_CLOSE:
            return struct.unpack("!H", data[:2])[0]


def test_browser_join_code_as_subprotocol() -> None:
    f = FeedServer({"bind": "127.0.0.1", "port": 0, "join_code": "XXX-XXX"}, HELLO)
    f.start()
    try:
        page = ["Origin: http://100.64.0.1:7320"]  # any origin: the code is the protection
        ok = WS(f.port, extra=page, protocols=f"{PROTOCOL}, {CODE_PROTOCOL}XXXXXX")
        assert ok.status == 101 and f"Sec-WebSocket-Protocol: {PROTOCOL}\r\n" in ok.headers
        assert ok.json("hello")["v"] == FEED_VERSION
        bad = WS(f.port, extra=page, protocols=f"{PROTOCOL}, {CODE_PROTOCOL}XXXXXA")
        assert bad.status == 101 and close_code(bad) == CLOSE_WRONG_CODE
        for _ in range(LOCKOUT_TRIES):
            WS(f.port, extra=page, protocols=f"{PROTOCOL}, {CODE_PROTOCOL}XXXXXA")
        locked = WS(f.port, extra=page, protocols=f"{PROTOCOL}, {CODE_PROTOCOL}XXXXXX")
        assert close_code(locked) == CLOSE_LOCKED_OUT
        assert WS(f.port, "XXXXXX").status == 429  # native clients still get HTTP statuses
    finally:
        f.stop()


def test_missing_code_not_counted() -> None:
    f = FeedServer({"bind": "127.0.0.1", "port": 0, "join_code": "XXX-XXX"}, HELLO)
    f.start()
    try:
        for _ in range(LOCKOUT_TRIES + 2):
            assert WS(f.port).status == 401
        assert WS(f.port, "XXXXXX").status == 101
    finally:
        f.stop()


def test_origin_checked_without_join_code(feed: FeedServer) -> None:
    port = feed.port
    assert WS(port, extra=["Origin: https://example.com"]).status == 403
    assert WS(port, extra=["Origin: null"]).status == 403
    assert WS(port, extra=[f"Origin: http://127.0.0.1:{port + 1}"]).status == 403
    assert WS(port, extra=[f"Origin: http://127.0.0.1:{port}"]).status == 101
    assert WS(port, extra=[f"Origin: http://localhost:{port}"]).status == 101
    # another copy's page on this machine lists this copy too; just outside the range it may not
    assert WS(port, extra=["Origin: http://127.0.0.1:7321"]).status == 101
    assert WS(port, extra=["Origin: http://127.0.0.1:7340"]).status == 403


def test_dns_rebinding_refused(feed: FeedServer) -> None:
    # a site whose name now points at 127.0.0.1 sends a matching Host; the origin is still not ours
    port = feed.port
    c = WS(port, extra=[f"Origin: http://evil.example:{port}", f"Host: evil.example:{port}"])
    assert c.status == 403


def test_build_in_hello(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "build" in FeedServer({"bind": "127.0.0.1", "port": 0}, HELLO).hello

    def no_git(*_a: object, **_k: object) -> None:
        raise OSError("no git")

    monkeypatch.setattr(feed_server.subprocess, "run", no_git)
    assert feed_server.build_id() is None


def test_pending_connections_capped() -> None:
    f = FeedServer({"bind": "127.0.0.1", "port": 0}, HELLO)
    f.start()
    try:
        idle = [socket.create_connection(("127.0.0.1", f.port))
                for _ in range(feed_server.MAX_PENDING)]
        wait_for(lambda: f.pending == feed_server.MAX_PENDING)
        extra = socket.create_connection(("127.0.0.1", f.port))
        extra.settimeout(3)
        assert extra.recv(1) == b""  # closed at once, before the 10 s handshake timeout
        for s in idle + [extra]:
            s.close()
        wait_for(lambda: f.pending == 0)
    finally:
        f.stop()


def test_unreadable_page_is_refused(monkeypatch) -> None:
    f = FeedServer({"bind": "127.0.0.1", "port": 0, "web": True}, HELLO)
    f.start()
    try:
        def fail(self):
            raise PermissionError("denied")
        monkeypatch.setattr(Path, "read_bytes", fail)
        s = socket.create_connection(("127.0.0.1", f.port))
        s.settimeout(3)
        s.sendall(b"GET /index.html HTTP/1.1\r\nHost: x\r\n\r\n")
        reply = b""
        while chunk := s.recv(4096):
            reply += chunk
        assert reply.startswith(b"HTTP/1.1 404")  # answered and closed, not left open
        s.close()
    finally:
        f.stop()


def test_https_attempt_closed_at_once(feed: FeedServer) -> None:
    # a browser trying https first must fail fast, or it never falls back to http
    s = socket.create_connection(("127.0.0.1", feed.port))
    s.settimeout(3)
    t = time.monotonic()
    s.sendall(b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03")  # start of a TLS ClientHello
    assert s.recv(1) == b""
    assert time.monotonic() - t < 1.0
    s.close()
    assert WS(feed.port).status == 101  # plain clients still served


def test_status_cpu_is_a_minute_average(monkeypatch: pytest.MonkeyPatch) -> None:
    # decoding is bursty: a busy 2 s must not read as the minute's load
    clock = {"t": 0.0, "cpu": 0.0}
    monkeypatch.setattr(feed_server.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(feed_server.os, "times", lambda: (clock["cpu"], 0.0, 0.0, 0.0, 0.0))
    stats = feed_server.ProcessStats()
    stats.clock_thread = threading.Thread()  # no clock check in a test
    pct = None
    for _ in range(60):  # two minutes: 2 s of a full core in every 30 s, idle otherwise
        clock["t"] += 2.0
        clock["cpu"] += 2.0 if clock["t"] % 30 == 2 else 0.0
        pct = stats.sample()["cpu"]
    assert pct == 7
    clock["t"] += 2.0
    clock["cpu"] += 2.0
    assert stats.sample()["cpu"] < 15  # one more busy 2 s moves it a little, not to 100


def _free_pair() -> int:
    """A port p with p and p + 1 both free on loopback."""
    for _ in range(50):
        with socket.socket() as a:
            a.bind(("127.0.0.1", 0))
            p = a.getsockname()[1]
        try:
            with socket.socket() as a, socket.socket() as b:
                a.bind(("127.0.0.1", p))
                b.bind(("127.0.0.1", p + 1))
                return p
        except OSError:
            continue
    raise RuntimeError("no two free ports in a row")


def test_auto_port_takes_the_first_free_one(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _free_pair()
    monkeypatch.setattr(feed_server, "AUTO_PORTS", range(p, p + 2))
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", p))
        busy.listen()
        f = FeedServer({"bind": "127.0.0.1", "port": "auto"}, HELLO)
        f.start()
        try:
            assert f.server is not None and f.port == p + 1
        finally:
            f.stop()


def test_auto_port_says_when_none_is_free(monkeypatch: pytest.MonkeyPatch,
                                          caplog: pytest.LogCaptureFixture) -> None:
    p = _free_pair()
    monkeypatch.setattr(feed_server, "AUTO_PORTS", range(p, p + 2))
    with socket.socket() as a, socket.socket() as b:
        for s, port in ((a, p), (b, p + 1)):
            s.bind(("127.0.0.1", port))
            s.listen()
        f = FeedServer({"bind": "127.0.0.1", "port": "auto"}, HELLO)
        with caplog.at_level(logging.ERROR):
            f.start()
        try:
            assert f.server is None
            assert f"no free port in {p}-{p + 1}" in caplog.text
        finally:
            f.stop()


def test_7320_is_the_servers_and_auto_starts_after_it(caplog: pytest.LogCaptureFixture) -> None:
    assert FeedServer({"bind": "127.0.0.1"}, HELLO).ports[0] == 7321
    assert list(feed_server.AUTO_PORTS) == list(range(7321, 7340))
    with caplog.at_level(logging.WARNING):
        FeedServer({"bind": "127.0.0.1", "port": 7320}, HELLO)
    assert "is the server's" in caplog.text


def _control(c: WS, change: object, rid: object = "r1") -> None:
    c.send(OP_TEXT, json.dumps({"type": "control", "id": rid, "set": change}).encode())


def _next_text(c: WS) -> dict:
    while True:
        op, data = c.frame()
        if op == OP_TEXT:
            return json.loads(data)


def test_a_control_nothing_takes_is_not_supported(feed: FeedServer) -> None:
    c = WS(feed.port)
    c.json("snapshot")
    _control(c, {"band": "20m"})
    assert c.json("control_result") == {
        "type": "control_result", "id": "r1", "ok": False, "error": "not supported"
    }


def test_a_control_reaches_sparkgap_and_its_answer_only_the_asker(feed: FeedServer) -> None:
    asked: list[dict] = []

    def take(change: dict, reply: Any) -> None:
        asked.append(change)
        reply(True, "retuned to 20m")

    feed.on_control = take
    other = WS(feed.port)
    other.json("snapshot")
    c = WS(feed.port)
    c.json("snapshot")
    wait_for(lambda: feed.n_clients == 2)
    _control(c, {"band": "20m"}, rid=7)
    assert c.json("control_result") == {
        "type": "control_result", "id": 7, "ok": True, "message": "retuned to 20m"
    }
    assert asked == [{"band": "20m"}]
    feed.publish_spot(14021.2, "AF4PX", 18, 22)
    assert _next_text(other)["type"] == "spot"


@pytest.mark.parametrize("rid, change", [([1], {"band": "20m"}), ("x" * 65, {"band": "20m"})])
def test_a_control_without_a_usable_id_gets_no_answer(feed: FeedServer, rid: object, change: object) -> None:
    feed.on_control = lambda change, reply: reply(True, "done")
    c = WS(feed.port)
    c.json("snapshot")
    _control(c, change, rid=rid)
    _control(c, {"band": "20m"}, rid="r2")
    assert c.json("control_result")["id"] == "r2"


def test_a_control_without_settings_is_refused(feed: FeedServer) -> None:
    feed.on_control = lambda change, reply: reply(True, "done")
    c = WS(feed.port)
    c.json("snapshot")
    _control(c, [])
    assert c.json("control_result")["error"] == "set must be an object"


def test_new_bands_leave_nothing_of_the_old_ones(feed: FeedServer) -> None:
    feed.publish_window(7021.2, "CQ DE AF4PX AF4PX K", 22, 18.0, 0.4)
    feed.publish_spot(7021.2, "AF4PX", 18, 22)
    feed.publish_status({"bins": 3})
    c = WS(feed.port)
    c.json("snapshot")
    wait_for(lambda: feed.n_clients == 1)
    feed.set_bands([{"name": "20m", "center_khz": 14038.4, "span_khz": 96.0}])
    assert c.json("hello")["bands"] == [{"name": "20m", "center_khz": 14038.4, "span_khz": 96.0}]
    snap = c.json("snapshot")
    assert snap["windows"] == [] and snap["spots"] == [] and snap["signals"] == [] and snap["status"] is None
    late = WS(feed.port)
    assert late.json("hello")["bands"][0]["name"] == "20m" and late.json("snapshot")["windows"] == []
