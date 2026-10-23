#!/usr/bin/env python3
"""SparkGap server: runs this machine's SparkGap instances for SparkGap Web.

One per machine, always on.  It starts, stops and restarts the instances listed in the root's
`instances` file, changes a fixed list of their settings (applied by a restart), reports their
state, serves SparkGap Web, and passes each running copy's feed through to the browser, so a
copy's feed can stay on this machine's loopback and its join code never leaves it.

    python3 sparkgap_server.py [--root DIR]     (default $SPARKGAP_ROOT, else ~/sparkgap)

Its own settings are in <root>/server.json, written on first run with a new join code:
{"bind": "127.0.0.1", "port": 7320, "join_code": "..."}; "tls_cert"/"tls_key" for wss.
Protocol: docs/SERVER_PROTOCOL.md.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import concurrent.futures
import contextlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import socket
import ssl
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

import help_page
from feed_server import (
    CLOSE_LOCKED_OUT,
    CLOSE_WRONG_CODE,
    CODE_PROTOCOL,
    LOCKOUT_S,
    LOCKOUT_TRIES,
    MAX_CLIENT_MESSAGE,
    MAX_HEADER_BYTES,
    OP_CLOSE,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    SERVER_PORT,
    FeedServer,
    ProtocolError,
    accept_key,
    build_id,
    encode_frame,
    is_loopback,
    new_code,
    normalize_code,
    read_frame,
    show_code,
    unmask,
    valid_code,
)
from seat import Seat, SeatError

log = logging.getLogger("sparkgap.server")

SERVER_PROTOCOL = "sparkgap.server.v1"
FEED_PROTOCOL = "sparkgap.v1"
SERVER_VERSION = 1
DEFAULT_PORT = SERVER_PORT  # 7320: the copies' feeds take 7321-7339
SERVER_PATH = "/server"
FEED_PREFIX = "/feed/"
HELP_PATH = "/help"
STATE_EVERY_S = 2.0
PING_EVERY_S = 20.0
MAX_CLIENTS = 16
MAX_QUEUED_OPS = 8
MAX_PENDING = 32  # connections still sending their request: more are closed at once
LOG_KEPT = 200  # server log lines kept for the web console
REQUEST_WAIT_S = 5.0
CONTROL_WAIT_S = 20.0  # a live band change: the radio's replies, then a new decoder
COMMANDS = ("start", "stop", "restart", "set")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def default_root() -> Path:
    return Path(os.environ.get("SPARKGAP_ROOT") or Path.home() / "sparkgap")


def load_settings(root: Path) -> dict[str, Any]:
    """server.json, written on first run (readable by its owner only) with a new join code."""
    path = root / "server.json"
    if not path.exists():
        code = new_code()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"bind": "127.0.0.1", "port": DEFAULT_PORT, "join_code": code}, f, indent=2)
            f.write("\n")
        print(f"sparkgap_server: new join code {show_code(code)} in {path}", flush=True)
    cfg: Any = json.loads(path.read_text())
    if not isinstance(cfg, dict) or not valid_code(str(cfg.get("join_code", ""))):
        raise SystemExit(f"{path}: needs a join_code")
    return cfg


class LogRing(logging.Handler):
    """The server's last LOG_KEPT log lines, and each new one passed to `on_line` (any thread)."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: collections.deque[dict[str, Any]] = collections.deque(maxlen=LOG_KEPT)
        self.on_line: Any = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = {
                "utc": round(record.created, 3),
                "level": record.levelname,
                "msg": record.getMessage(),
            }
        except Exception:
            return
        self.lines.append(line)
        if self.on_line:
            with contextlib.suppress(RuntimeError):  # the loop closed: a line logged while stopping
                self.on_line(line)


class Client:
    def __init__(self, writer: asyncio.StreamWriter, peer: str) -> None:
        self.writer = writer
        self.peer = peer


class Server:
    def __init__(self, root: Path, cfg: dict[str, Any]) -> None:
        self.seat = Seat(root)
        self.log_ring = LogRing()
        logging.getLogger("sparkgap.server").addHandler(self.log_ring)
        logging.getLogger("sparkgap.server").setLevel(logging.INFO)  # what the console shows
        self.bind = str(cfg.get("bind", "127.0.0.1"))
        self.port = int(cfg.get("port", DEFAULT_PORT))
        self.code = normalize_code(str(cfg["join_code"]))
        self.tls: ssl.SSLContext | None = None
        if cfg.get("tls_cert"):
            self.tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            self.tls.load_cert_chain(cfg["tls_cert"], cfg.get("tls_key"))
        if not is_loopback(self.bind) and not self.tls:
            log.warning(
                "Server: %s is not loopback and has no TLS: reach it only over a "
                "trusted network (a VPN)",
                self.bind,
            )
        self.clients: set[Client] = set()
        self.misses: dict[str, collections.deque[float]] = {}
        self.ops: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(1)
        self.queued = 0
        self.pending = 0
        self.conns: set[asyncio.StreamWriter] = set()  # every open connection, closed at shutdown
        self.listener: asyncio.Server | None = None
        self.busy: dict[str, str] = {}  # instance -> operation in progress or queued
        self.state: dict[str, Any] = {}
        self.ready = threading.Event()  # set once listening (for tests and callers)
        self.hello = {
            "type": "hello",
            "v": SERVER_VERSION,
            "server": True,
            "host": socket.gethostname(),
            "build": build_id(),
        }

    # ---- running ----

    async def serve(self) -> None:
        server = await asyncio.start_server(self._handle, self.bind, self.port, ssl=self.tls)
        loop = asyncio.get_running_loop()
        self.log_ring.on_line = lambda line: loop.call_soon_threadsafe(
            self._broadcast, {"type": "log", "lines": [line]}
        )
        self.port = server.sockets[0].getsockname()[1]
        log.info(
            "Server on %s://%s:%d (SparkGap Web at http%s://%s:%d/), root %s",
            "wss" if self.tls else "ws",
            self.bind,
            self.port,
            "s" if self.tls else "",
            self.bind,
            self.port,
            self.seat.root,
        )
        self.ready.set()  # after the line above, which the web console shows
        asyncio.get_running_loop().create_task(self._state_loop())
        self.listener = server
        await server.serve_forever()

    def close_connections(self) -> None:
        """Stop listening and close every open connection.  Before serve() is cancelled: in Python
        3.12 a cancelled serve_forever waits for open connections, so a browser left connected
        kept the process alive after SIGTERM (Dallas, 2026-10-22).  Aborted, not closed: a close
        first sends what is queued, which a browser that stopped reading never takes (Beelink)."""
        if self.listener:
            self.listener.close()
        for w in list(self.conns):
            w.transport.abort()

    async def _state_loop(self) -> None:
        last_ping = time.monotonic()
        while True:
            try:
                self.state = await asyncio.to_thread(self._snapshot)
                self._broadcast(self.state)
            except Exception:
                log.exception("Server: state")
            if time.monotonic() - last_ping > PING_EVERY_S:
                last_ping = time.monotonic()
                for c in list(self.clients):
                    self._send(c, encode_frame(OP_PING, b""))
            await asyncio.sleep(STATE_EVERY_S)

    def _snapshot(self) -> dict[str, Any]:
        snap = self.seat.snapshot()
        for i in snap["instances"]:
            i.pop("feed_code", None)  # stays on this machine: the server proxies the feed
            i["busy"] = self.busy.get(i["name"])
        return {"type": "seat", "utc": time.time(), **snap}

    # ---- connections ----

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = str(writer.get_extra_info("peername")[0])
        if self.pending >= MAX_PENDING:  # before any code check: a crowd of half-open requests
            writer.close()
            return
        self.conns.add(writer)
        try:
            self.pending += 1
            try:
                req = await asyncio.wait_for(self._request(reader), REQUEST_WAIT_S)
            finally:
                self.pending -= 1
            if req is None:
                return
            method, path, headers = req
            if headers.get("upgrade", "").lower() != "websocket":
                if method == "GET" and (path == HELP_PATH or path.startswith(HELP_PATH + "/")):
                    self._serve_help(writer, path[len(HELP_PATH) + 1 :] or help_page.GUIDE)
                elif method == "GET" and not path.startswith((SERVER_PATH, FEED_PREFIX)):
                    FeedServer._serve_file(writer, path)  # pyright: ignore[reportPrivateUsage]
                else:
                    FeedServer._reject(writer, 400, "Bad Request")  # pyright: ignore[reportPrivateUsage]
                return
            offered = [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",")]
            given = next(
                (p[len(CODE_PROTOCOL) :] for p in offered if p.startswith(CODE_PROTOCOL)),
                headers.get("authorization", "").removeprefix("Bearer ").strip(),
            )
            if path != SERVER_PATH and not path.startswith(FEED_PREFIX):
                # a copy-port search of this address lands here: not ours, so not a wrong code
                FeedServer._reject(writer, 404, "Not Found")  # pyright: ignore[reportPrivateUsage]
                return
            ok = self._code_ok(peer, given)
            if path == SERVER_PATH:
                await self._server_client(reader, writer, headers, offered, ok, peer)
            elif path.startswith(FEED_PREFIX):
                await self._proxy(reader, writer, headers, offered, ok, path[len(FEED_PREFIX) :])
            else:
                FeedServer._reject(writer, 404, "Not Found")  # pyright: ignore[reportPrivateUsage]
        except (
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
            ConnectionError,
            ProtocolError,
            OSError,
        ):
            pass
        finally:
            self.conns.discard(writer)
            with contextlib.suppress(Exception):
                await writer.drain()
            writer.close()

    @staticmethod
    def _serve_help(writer: asyncio.StreamWriter, name: str) -> None:
        """The guide shipped with this code (docs/), so Help matches the version running."""
        found = help_page.page(name)
        if found is None:
            FeedServer._reject(writer, 404, "Not Found")  # pyright: ignore[reportPrivateUsage]
            return
        body, kind = found
        head = [
            "HTTP/1.1 200 OK",
            f"Content-Type: {kind}",
            f"Content-Length: {len(body)}",
            "Cache-Control: no-cache",
            "X-Content-Type-Options: nosniff",
            "Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline'",
            "Connection: close",
        ]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + body)

    @staticmethod
    async def _request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str]] | None:
        raw = await reader.readuntil(b"\r\n\r\n")
        if len(raw) > MAX_HEADER_BYTES:
            return None
        lines = raw.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            return None
        headers = {}
        for line in lines[1:]:
            k, sep, v = line.partition(":")
            if sep:
                headers[k.strip().lower()] = v.strip()
        return parts[0], parts[1].split("?")[0], headers

    def _code_ok(self, host: str, given: str) -> bool | None:
        """True for the right code, False for a wrong one, None while host is locked out."""
        now = time.monotonic()
        misses = self.misses.get(host, collections.deque())
        while misses and now - misses[0] > LOCKOUT_S:
            misses.popleft()
        if len(misses) >= LOCKOUT_TRIES:
            return None
        if hmac.compare_digest(normalize_code(given).encode(), self.code.encode()):
            return True
        if normalize_code(given):
            misses.append(now)
            self.misses[host] = misses
            if len(misses) == LOCKOUT_TRIES:
                log.warning(
                    "Server: %s locked out for %d min after %d wrong join codes",
                    host,
                    LOCKOUT_S // 60,
                    LOCKOUT_TRIES,
                )
        return False

    @staticmethod
    def _accept(
        writer: asyncio.StreamWriter, headers: dict[str, str], protocol: str | None
    ) -> None:
        reply = [
            "HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Accept: {accept_key(headers.get('sec-websocket-key', ''))}",
        ]
        if protocol:
            reply.append(f"Sec-WebSocket-Protocol: {protocol}")
        writer.write(("\r\n".join(reply) + "\r\n\r\n").encode())

    @staticmethod
    def _refuse(writer: asyncio.StreamWriter, ok: bool | None, why: str = "") -> None:
        """A browser sees only the close code: accept the upgrade, then close with the reason."""
        code = CLOSE_LOCKED_OUT if ok is None else CLOSE_WRONG_CODE if ok is False else 1011
        reason = {CLOSE_LOCKED_OUT: "locked out", CLOSE_WRONG_CODE: "wrong join code"}.get(
            code, why
        )
        writer.write(encode_frame(OP_CLOSE, struct.pack("!H", code) + reason.encode()[:120]))

    # ---- the server protocol ----

    async def _server_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        offered: list[str],
        ok: bool | None,
        peer: str,
    ) -> None:
        if SERVER_PROTOCOL not in offered:
            FeedServer._reject(writer, 400, "Bad Request")  # pyright: ignore[reportPrivateUsage]
            return
        self._accept(writer, headers, SERVER_PROTOCOL)
        if not ok or len(self.clients) >= MAX_CLIENTS:
            self._refuse(writer, ok if not ok else True, "too many clients")
            return
        c = Client(writer, peer)
        self.clients.add(c)
        log.info("Server: %s connected", peer)
        try:
            self._send_json(c, self.hello)
            self._send_json(c, {"type": "log", "reset": True, "lines": list(self.log_ring.lines)})
            if self.state:
                self._send_json(c, self.state)
            while True:
                fin, op, data = await read_frame(reader)
                if op == OP_CLOSE:
                    return
                if op == OP_PING:
                    self._send(c, encode_frame(OP_PONG, data))
                elif op == OP_TEXT and fin and len(data) <= MAX_CLIENT_MESSAGE:
                    self._request_op(c, data)
        finally:
            self.clients.discard(c)
            log.info("Server: %s left", peer)

    def _request_op(self, c: Client, data: bytes) -> None:
        try:
            msg: Any = json.loads(data)
        except ValueError:
            return
        if not isinstance(msg, dict) or msg.get("type") != "server":
            return
        rid, cmd, name = msg.get("id"), msg.get("cmd"), msg.get("name")
        settings = msg.get("settings") or {}

        def result(ok: bool, text: str) -> None:
            self._send_json(
                c,
                {
                    "type": "server_result",
                    "id": rid,
                    "ok": ok,
                    ("message" if ok else "error"): text,
                },
            )

        if cmd not in COMMANDS or not isinstance(name, str) or not NAME_RE.match(name):
            return result(False, "unknown command or instance")
        if cmd == "set" and not isinstance(settings, dict):
            return result(False, "settings must be an object")
        if name in self.busy:
            return result(False, f"{name} is busy: {self.busy[name]}")
        if self.queued >= MAX_QUEUED_OPS:
            return result(False, "too many requests waiting")
        band_only = cmd == "set" and set(settings) == {"band"}
        self.busy[name] = (
            "changing band" if band_only else
            {"set": "restarting"}.get(cmd, cmd + ("ing" if cmd != "stop" else "ping"))
        )
        self.queued += 1
        log.info("Server: %s asks %s %s%s", c.peer, cmd, name, f" {settings}" if settings else "")
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self.ops, self._run_op, cmd, name, settings)

        def done(f: asyncio.Future[str]) -> None:
            self.queued -= 1
            self.busy.pop(name, None)
            err = f.exception()
            if err is None:
                log.info("Server: %s", f.result())
                result(True, f.result())
            else:
                log.warning("Server: %s %s failed: %s", cmd, name, err)
                result(False, str(err))

        fut.add_done_callback(done)
        return None

    def _run_op(self, cmd: str, name: str, settings: dict[str, Any]) -> str:
        if cmd == "start":
            return self.seat.start(name)
        if cmd == "stop":
            return self.seat.stop(name)
        if cmd == "restart":
            return self.seat.restart(name)
        return self.seat.change(name, settings, live=self._live_band)

    def _live_band(self, name: str, band: str) -> str | None:
        """Ask the running copy to move to band in place; its answer, or None to restart it."""
        target = self._feed_target(name)
        if target is None:
            return None
        answer = feed_control(*target, {"band": band})
        if answer is None:
            log.warning("Server: %s gave no answer to a live band change: restarting it", name)
            return None
        ok, text = answer
        if not ok:
            log.info("Server: %s did not change band live (%s): restarting it", name, text)
            return None
        return text

    def _send_json(self, c: Client, msg: dict[str, Any]) -> None:
        self._send(c, encode_frame(OP_TEXT, json.dumps(msg).encode()))

    def _broadcast(self, msg: dict[str, Any]) -> None:
        frame = encode_frame(OP_TEXT, json.dumps(msg).encode())
        for c in list(self.clients):
            self._send(c, frame)

    def _send(self, c: Client, frame: bytes) -> None:
        if (
            c.writer.transport.get_write_buffer_size() > 4 * 2**20
        ):  # a client this far behind leaves
            c.writer.close()
            self.clients.discard(c)
            return
        c.writer.write(frame)

    # ---- a copy's feed, passed through ----

    async def _proxy(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        offered: list[str],
        ok: bool | None,
        name: str,
    ) -> None:
        """The browser's feed for copy `name`: the server opens the copy's feed on this machine
        with the copy's own join code, then passes frames through unchanged both ways."""
        protocol = FEED_PROTOCOL if FEED_PROTOCOL in offered else None
        self._accept(writer, headers, protocol)
        if not ok:
            self._refuse(writer, ok)
            return
        target = await asyncio.to_thread(self._feed_target, name)
        if target is None:
            self._refuse(writer, True, f"{name} has no feed running")
            return
        host, port, code = target
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
        except (OSError, asyncio.TimeoutError):
            self._refuse(writer, True, f"{name}: no answer on its feed port")
            return
        up_w.write(feed_request(host, port, code))
        try:
            reply = await asyncio.wait_for(up_r.readuntil(b"\r\n\r\n"), 5)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            reply = b""
        if not reply.startswith(b"HTTP/1.1 101"):
            up_w.close()
            self._refuse(writer, True, f"{name}: its feed refused the server")
            return

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            with contextlib.suppress(Exception):
                while chunk := await src.read(65_536):
                    dst.write(chunk)
                    await dst.drain()

        # either side ending ends both: a half-open copy must not hold the browser's connection
        tasks = [
            asyncio.ensure_future(pipe(reader, up_w)),
            asyncio.ensure_future(pipe(up_r, writer)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            up_w.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(up_w.wait_closed(), 2)

    def _feed_target(self, name: str) -> tuple[str, int, str | None] | None:
        try:
            i = self.seat.instance(name)
        except SeatError:
            return None
        st = self.seat.status(i)
        if st["state"] != "running" or not st["feed_port"]:
            return None
        bind = str((self.seat.config(i).get("gui_feed") or {}).get("bind", "127.0.0.1"))
        if bind not in ("0.0.0.0", "::", "") and not is_loopback(bind):
            log.warning("Server: %s's feed is bound to %s, not this machine only: not passed "
                        "through (its join code would cross the network)", name, bind)
            return None
        return "127.0.0.1", int(st["feed_port"]), st["feed_code"]


def feed_request(host: str, port: int, code: str | None) -> bytes:
    """The opening request for a copy's feed on this machine, with the copy's own join code."""
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    req = [
        "GET /feed HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        f"Sec-WebSocket-Protocol: {FEED_PROTOCOL}",
    ]
    if code:
        req.append(f"Authorization: Bearer {code}")
    return ("\r\n".join(req) + "\r\n\r\n").encode()


def client_frame(opcode: int, payload: bytes) -> bytes:
    """Client frame: FIN set, masked (a feed refuses unmasked ones)."""
    mask = secrets.token_bytes(4)
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
    elif n < 65_536:
        head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
    return head + mask + unmask(payload, mask)


def read_server_frame(f: Any) -> tuple[int, bytes]:
    """One unmasked frame from a feed: (opcode, payload). EOFError when the feed closes."""

    def exactly(n: int) -> bytes:
        data = f.read(n)
        if len(data) < n:
            raise EOFError
        return data

    b0, b1 = exactly(2)
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", exactly(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", exactly(8))
    return b0 & 0x0F, exactly(n)


def feed_control(
    host: str, port: int, code: str | None, change: dict[str, Any], wait_s: float = CONTROL_WAIT_S
) -> tuple[bool, str] | None:
    """Send one control message to a copy's feed and wait for its answer: (ok, text), or None
    when there is none (no feed, refused, closed, timed out). Blocks: run it off the event loop."""
    rid = secrets.token_hex(8)
    end = time.monotonic() + wait_s
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(feed_request(host, port, code))
            f = sock.makefile("rb")
            if not f.readline().startswith(b"HTTP/1.1 101"):
                return None
            while f.readline() not in (b"\r\n", b""):
                pass
            msg = {"type": "control", "id": rid, "set": change}
            sock.sendall(client_frame(OP_TEXT, json.dumps(msg).encode()))
            while (left := end - time.monotonic()) > 0:
                sock.settimeout(left)
                op, data = read_server_frame(f)
                if op == OP_PING:
                    sock.sendall(client_frame(OP_PONG, data))
                elif op == OP_CLOSE:
                    return None
                elif op == OP_TEXT:
                    reply = json.loads(data)
                    if reply.get("type") == "control_result" and reply.get("id") == rid:
                        text = reply.get("message") or reply.get("error")
                        return bool(reply.get("ok")), str(text)
    except (OSError, EOFError, ValueError, AttributeError):
        return None
    return None


async def _serve_until_signalled(server: Server) -> None:
    """Serve until SIGINT or SIGTERM, with our own handlers: a process started in the background
    by a non-interactive shell (over ssh, say) inherits SIGINT ignored."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    task = asyncio.ensure_future(server.serve())
    await asyncio.wait(
        [task, asyncio.ensure_future(stop.wait())], return_when=asyncio.FIRST_COMPLETED
    )
    server.close_connections()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SparkGap server: runs this machine's instances")
    ap.add_argument("--root", type=Path, default=default_root(), help="the run folder")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    if not (args.root / "instances").exists():
        raise SystemExit(f"{args.root}: no instances file (docs/SERVER_SETUP.md)")
    server = Server(args.root, load_settings(args.root))
    pidfile = args.root / "logs" / "server.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(f"{os.getpid()}\n")
    try:
        asyncio.run(_serve_until_signalled(server))
    finally:
        with contextlib.suppress(OSError):
            if pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
    if server.queued:  # its worker would hold the exit for as long as a start waits (45 s)
        under_way = ", ".join(f"{n} {b}" for n, b in server.busy.items())
        log.warning("Server: stopping with %s under way", under_way)
        logging.shutdown()
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
