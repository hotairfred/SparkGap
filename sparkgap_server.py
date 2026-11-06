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
import ipaddress
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
    FOR_QUERY,
    LOCKOUT_S,
    LOCKOUT_TRIES,
    MAX_CLIENT_MESSAGE,
    MAX_HEADER_BYTES,
    OP_CLOSE,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    RECOVERING,
    SERVER_PORT,
    VIEW_QUERY,
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
from process_link import PROCESS_PATH, PROCESS_PROTOCOL
from seat import RBN_MODES, RX_SETTINGS, Instance, Seat, SeatError, station_view

log = logging.getLogger("sparkgap.server")
# a station's state in the server log, in the words the status bar uses
RADIO_STATES = {"live": "connected", "connecting": "connecting", "reconnecting": "reconnecting",
                "halted": "not connected (halted)", "stopped": "not connected"}

SERVER_PROTOCOL = "sparkgap.server.v1"
FEED_PROTOCOL = "sparkgap.v1"
SERVER_VERSION = 1
DEFAULT_PORT = SERVER_PORT  # 7320: the copies' feeds take 7321-7329, their telnet 7330 + DAX-IQ channel
SERVER_PATH = "/server"
FEED_PREFIX = "/feed/"
HELP_PATH = "/help"
STATE_EVERY_S = 2.0
PING_EVERY_S = 20.0
MAX_CLIENTS = 16
MAX_PROXIED = 32  # browser feeds passed through to copies, all copies together
MAX_QUEUED_OPS = 8
MAX_PENDING = 32  # connections still sending their request: more are closed at once
LOG_KEPT = 200  # server log lines kept for the web console
REQUEST_WAIT_S = 5.0
CONTROL_WAIT_S = 20.0  # a live band change: the radio's replies, then a new decoder
FEED_LINK_S = 20.0  # longest an RBN feeder takes to start and link to the hub
FEED_STOP_S = 30.0
FEED_RELINK_S = 12.0  # a running feeder links again within its RECONNECT_S after a server restart
COMMANDS = ("start", "stop", "restart", "set", "add", "remove")
RADIO_COMMANDS = ("discover", "node")  # not tied to one instance
BY_RE = re.compile(r"[^A-Za-z0-9 ._-]")  # what a client may call itself in the log


def requester(by: Any, peer: str) -> str:
    """Who asked, for the logs: the client's own name (SparkGap Web, sparkgap.sh) and its address."""
    name = BY_RE.sub("", str(by or ""))[:32].strip() or "a client"
    return f"{name} ({peer})"


def what_asked(cmd: str, name: str, settings: dict[str, Any]) -> str:
    """A request in words: stop Dallas-2; set Dallas-1 rf_gain 0; node Lake callsign N0CALL-2."""
    shown = ", ".join(f"{k} {v}" for k, v in settings.items()) if settings else ""
    target = "" if name == cmd else f" {name}"
    return f"{cmd}{target}{f' {shown}' if shown else ''}"
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
PROC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")  # a station may be an address


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


def own_addresses(bind: str, hostname: str | None = None) -> list[str]:
    """Loopback addresses also served when `bind` is a network address, so a browser on this
    machine reaches the server by localhost or by the machine's own name (127.0.1.1 on Ubuntu)."""
    if bind in ("0.0.0.0", "::", "") or is_loopback(bind):
        return []
    found = ["127.0.0.1"]
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(hostname or socket.gethostname(), None, socket.AF_INET):
            addr = str(info[4][0])
            if is_loopback(addr) and addr not in found:
                found.append(addr)
    return found


def busy_word(cmd: str, settings: dict[str, Any]) -> str:
    """What a receiver or radio shows while a request for it runs."""
    if cmd == "set" and set(settings) == {"rbn_upload"}:
        return "changing its rbn feeder"
    if cmd == "set" and set(settings) == {"band"}:
        return "changing band"
    if cmd == "set" and settings and set(settings) <= set(RX_SETTINGS):
        return "changing the receive path"
    return {"set": "restarting", "add": "adding", "remove": "removing", "stop": "stopping",
            "node": "saving the node", "discover": "discovering"}.get(cmd, cmd + "ing")


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


class Proc:
    """A process linked to the /process hub: who it is (its hello) and its last status."""

    def __init__(self, writer: asyncio.StreamWriter, hello: dict[str, Any]) -> None:
        self.writer = writer
        self.role, self.name = str(hello.get("role")), str(hello.get("name"))
        self.pid = hello.get("pid")
        self.pgid = hello.get("pgid")
        self.since = time.time()
        self.status: dict[str, Any] = {}
        self.pending: dict[str, asyncio.Future[tuple[bool, str]]] = {}

    def view(self) -> dict[str, Any]:
        return {"role": self.role, "name": self.name, "pid": self.pid, "since": self.since,
                "status": self.status}


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
        self.proxied = 0
        self.conns: set[asyncio.StreamWriter] = set()  # every open connection, closed at shutdown
        self.listener: asyncio.Server | None = None
        self.local_listener: asyncio.Server | None = None  # loopback, for a network bind
        self.busy: dict[str, str] = {}  # instance -> operation in progress or queued
        self.procs: dict[tuple[str, str], Proc] = {}  # (role, name) -> its link to the hub
        self.loop: asyncio.AbstractEventLoop | None = None
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
        loop = self.loop = asyncio.get_running_loop()
        self.log_ring.on_line = lambda line: loop.call_soon_threadsafe(
            self._broadcast, {"type": "log", "lines": [line]}
        )
        self.port = server.sockets[0].getsockname()[1]
        local = own_addresses(self.bind)
        if local:
            try:
                self.local_listener = await asyncio.start_server(
                    self._handle, local, self.port, ssl=self.tls
                )
                log.info("Server also on %s for this machine", ", ".join(local))
            except OSError as e:
                log.warning("Server: not on %s for this machine: %s", ", ".join(local), e)
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
        for listener in (self.listener, self.local_listener):
            if listener:
                listener.close()
        for w in list(self.conns):
            w.transport.abort()

    async def _state_loop(self) -> None:
        last_ping = time.monotonic()
        while True:
            try:
                if self.clients:
                    self.state = await asyncio.to_thread(self._snapshot)
                    self._broadcast(self.state)
                else:
                    self.state = {}  # nobody watching: no reads, nothing stale for the next one
            except Exception:
                log.exception("Server: state")
            try:
                await self._reap()
            except Exception:
                log.exception("Server: feeders")
            if time.monotonic() - last_ping > PING_EVERY_S:
                last_ping = time.monotonic()
                for c in list(self.clients):
                    self._send(c, encode_frame(OP_PING, b""))
                for p in list(self.procs.values()):  # a process that died without closing goes
                    p.writer.write(encode_frame(OP_PING, b""))
            await asyncio.sleep(STATE_EVERY_S)

    def _snapshot(self) -> dict[str, Any]:
        snap = self.seat.snapshot()
        for i in snap["instances"]:
            i.pop("feed_code", None)  # stays on this machine: the server proxies the feed
            i["busy"] = self.busy.get(i["name"])
            self._linked(i)
        for s in snap["stations"]:
            if p := self.procs.get(("station", s["radio"])):
                s.update(station_view(s["radio"], True, p.status))
        for r in snap["radios"]:
            p = self.procs.get(("feeder", r["name"]))
            r["feeder"] = p.status if p else None
        procs = [p.view() for p in list(self.procs.values())]
        return {"type": "seat", "utc": time.time(), **snap, "processes": procs}

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
            if path == PROCESS_PATH:  # a process of this machine: no join code, loopback only
                await self._process_client(reader, writer, headers, offered, peer)
                return
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
            asyncio.LimitOverrunError,
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
        for h in [h for h, m in self.misses.items() if not m or now - m[-1] > LOCKOUT_S]:
            del self.misses[h]
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

    # ---- the process hub: this machine's copies, stations and feeders link here ----

    async def _process_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        offered: list[str],
        peer: str,
    ) -> None:
        if not is_loopback(peer) or PROCESS_PROTOCOL not in offered:
            FeedServer._reject(writer, 403, "Forbidden")  # pyright: ignore[reportPrivateUsage]
            return
        self._accept(writer, headers, PROCESS_PROTOCOL)
        fin, op, data = await asyncio.wait_for(read_frame(reader), REQUEST_WAIT_S)
        hello = json.loads(data) if op == OP_TEXT and fin else {}
        if hello.get("type") != "hello" or not PROC_NAME_RE.match(str(hello.get("name", ""))):
            return
        p = Proc(writer, hello)
        old = self.procs.get((p.role, p.name))
        if old is not None:  # the same process again after a lost link, or a second copy of it
            old.writer.close()
        self.procs[(p.role, p.name)] = p
        log.info("Server: %s %s linked (pid %s)", p.role, p.name, p.pid)
        try:
            while True:
                fin, op, data = await read_frame(reader)
                if op == OP_CLOSE:
                    return
                if op == OP_PING:
                    writer.write(encode_frame(OP_PONG, data))
                elif op == OP_TEXT and fin and len(data) <= MAX_CLIENT_MESSAGE:
                    msg = json.loads(data)
                    if msg.get("type") == "status":
                        was = p.status.get("state")
                        p.status = {k: v for k, v in msg.items() if k != "type"}
                        if p.role == "station" and p.status.get("state") != was:
                            log.info("Server: radio %s %s", p.name, RADIO_STATES.get(
                                str(p.status.get("state")), str(p.status.get("state"))))
                    elif msg.get("type") == "log":
                        level = logging.ERROR if msg.get("level") in ("ERROR", "CRITICAL") else logging.WARNING
                        log.log(level, "%s %s: %s", p.role.capitalize(), p.name, str(msg.get("msg")))
                    elif msg.get("type") == "result":
                        f = p.pending.pop(str(msg.get("id")), None)
                        if f is not None and not f.done():
                            text = str(msg.get("message") or msg.get("error"))
                            f.set_result((bool(msg.get("ok")), text))
        finally:
            if self.procs.get((p.role, p.name)) is p:
                del self.procs[(p.role, p.name)]
                log.info("Server: %s %s unlinked", p.role, p.name)
            for f in p.pending.values():
                if not f.done():
                    f.set_result((False, f"{p.role} {p.name} went away"))

    async def command(
        self, role: str, name: str, cmd: str, args: dict[str, Any] | None = None,
        wait_s: float = CONTROL_WAIT_S,
    ) -> tuple[bool, str] | None:
        """Send a command to a linked process and wait for its answer; None when it is not linked
        or does not answer in time."""
        p = self.procs.get((role, name))
        if p is None:
            return None
        rid = secrets.token_hex(8)
        fut: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
        p.pending[rid] = fut
        msg = {"type": "command", "id": rid, "cmd": cmd, "args": args or {}}
        p.writer.write(encode_frame(OP_TEXT, json.dumps(msg).encode()))
        try:
            return await asyncio.wait_for(fut, wait_s)
        except asyncio.TimeoutError:
            p.pending.pop(rid, None)
            return None

    def command_sync(
        self, role: str, name: str, cmd: str, args: dict[str, Any] | None = None,
        wait_s: float = CONTROL_WAIT_S,
    ) -> tuple[bool, str] | None:
        """command() from a worker thread (the seat operations run off the event loop)."""
        if self.loop is None:
            return None
        coro = self.command(role, name, cmd, args, wait_s)
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(wait_s + 5)

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

        def refuse(text: str) -> None:  # logged, so the web's footer and log show it too
            log.warning("Server: %s %s refused: %s", cmd, name, text)
            result(False, text)

        if cmd in RADIO_COMMANDS:
            name = cmd   # busy and queued like an instance, under its own name
        elif cmd not in COMMANDS or not isinstance(name, str) or not NAME_RE.match(name):
            return refuse("unknown command or instance")
        if cmd in ("set", "add", "discover", "node") and not isinstance(settings, dict):
            return refuse("settings must be an object")
        if name in self.busy:
            return refuse(f"{name} is busy: {self.busy[name]}")
        if self.queued >= MAX_QUEUED_OPS:
            return refuse("too many requests waiting")
        self.busy[name] = busy_word(cmd, settings)
        self.queued += 1
        who = requester(msg.get("by"), c.peer)
        log.info("Server: %s by %s", what_asked(cmd, name, settings), who)
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self.ops, self._run_op, cmd, name, settings, who)

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

    def _run_op(self, cmd: str, name: str, settings: dict[str, Any], who: str = "") -> str:
        if who and cmd in COMMANDS:  # the receiver's own log says who asked, before it acts
            with contextlib.suppress(Exception):  # a receiver not linked (stopped) has no log to tell
                self.command_sync("skimmer", name, "note",
                                  {"text": f"{what_asked(cmd, name, settings)} by {who}"}, 2.0)
        if cmd == "discover":
            address = settings.get("address")
            if address is not None:
                ipaddress.ip_address(address)  # ValueError: refused, nothing asked
            return self.seat.discover(address=address)
        if cmd == "node":  # sparkgap.sh setup's spotter ID, grid and operator, from the web
            keys = ("radio", "callsign", "grid", "op_name")
            return self.seat.set_node(*(str(settings.get(k) or "") for k in keys))
        if cmd == "add":  # the rest of its settings are checked and written as a stopped one's
            added = self.seat.add(name, str(settings.get("band") or ""))
            rest = {key: v for key, v in settings.items() if key != "band"}
            try:
                if rest:
                    self.seat.change(name, rest)
            except SeatError:
                self.seat.remove(name)  # not added after all: its config stays, as on a remove
                raise
            return self._with(added, self.seat.start(name), self._feeder_up(name))
        if cmd == "remove":
            down = self._feeder_down(name) if self._running(name) else ""
            return self._with(down, self.seat.stop(name) if self._running(name) else "",
                              self.seat.remove(name))
        if cmd == "start":
            return self._with(self.seat.start(name), self._feeder_up(name))
        if cmd == "stop":
            return self._with(self._feeder_down(name), self.seat.stop(name))
        if cmd == "restart":
            down = self._feeder_down(name)
            return self._with(down, self.seat.restart(name), self._feeder_up(name))
        if cmd == "set" and set(settings) == {"rbn_upload"}:
            return self._set_upload(name, settings["rbn_upload"])
        return self.seat.change(name, settings, live=self._live_band, live_rx=self._live_rx)

    # ---- the RBN feeder, through its link to the hub ----

    @staticmethod
    def _with(*parts: str) -> str:
        return "; ".join(p for p in parts if p)

    def _wait_link(self, name: str, linked: bool, wait_s: float) -> bool:
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            if (("feeder", name) in self.procs) == linked:
                return True
            time.sleep(0.2)
        return False

    def _running(self, name: str) -> bool:
        return ("skimmer", name) in self.procs or bool(self.seat.pid(name))

    def _feeds(self, i: Instance, without: str | None = None) -> list[str]:
        """The running instances that feed i's feeder: the radio's receivers, or i alone."""
        fname = self.seat.feeder_name(i)
        return [j.name for j in self.seat.instances() if j.name != without
                and self.seat.feeder_name(j) == fname and self._running(j.name)]

    def _feeder_up(self, name: str) -> str:
        """Start name's feeder when its rbn_upload is dry or live and it runs; a radio's feeder
        that runs already is told its receivers."""
        i = self.seat.instance(name)
        mode = self.seat.upload_mode(i)
        if mode == "off" or not self._running(name):
            return ""
        fname = self.seat.feeder_name(i)
        if ("feeder", fname) not in self.procs and self.seat.feeder_pid(fname):
            # running but not linked yet (the server restarted): never a second feeder
            if not self._wait_link(fname, True, FEED_RELINK_S):
                pid = self.seat.feeder_pid(fname)
                return f"rbn feeder {fname} runs (pid {pid}) but has not linked"
        if ("feeder", fname) in self.procs:
            return self._tell_receivers(i) or "rbn feeder already running"
        log_path = self.seat.spawn_feeder(i, self._feeds(i))
        if self._wait_link(fname, True, FEED_LINK_S):
            return f"rbn feeder {mode}"
        return f"rbn feeder did not link within {FEED_LINK_S:.0f} s, see {log_path.name}"

    def _tell_receivers(self, i: Instance, without: str | None = None) -> str:
        if self.seat.radio_file(i) is None:
            return ""
        names = self._feeds(i, without)
        answer = self.command_sync("feeder", self.seat.feeder_name(i), "receivers",
                                   {"names": names}, wait_s=5.0)
        return answer[1] if answer else "rbn feeder gave no answer"

    def _feeder_down(self, name: str, all_receivers: bool = False) -> str:
        """name stops: its feeder stops, unless other receivers of its radio still feed it (they
        are its receivers now) or all_receivers; through its link, its process group if it does
        not go."""
        i = self.seat.instance(name)
        fname = self.seat.feeder_name(i)
        p = self.procs.get(("feeder", fname))
        if p is None:
            return ""
        if not all_receivers and self._feeds(i, without=name):
            return self._tell_receivers(i, without=name)
        self.command_sync("feeder", fname, "stop", wait_s=5.0)
        if self._wait_link(fname, False, FEED_STOP_S):
            return "rbn feeder stopped"
        if isinstance(p.pgid, int):
            with contextlib.suppress(OSError):
                os.killpg(p.pgid, signal.SIGTERM)
        if self._wait_link(fname, False, 5.0):
            return "rbn feeder stopped (it did not answer: signalled)"
        raise SeatError(f"{name}: rbn feeder still running (pid {p.pid})")

    def _set_upload(self, name: str, mode: Any) -> str:
        """rbn_upload off, dry or live, for the radio's node (all its receivers) or the instance:
        only the feeder follows it; the copies keep running."""
        if mode not in RBN_MODES:
            raise SeatError(f"rbn_upload must be one of: {', '.join(RBN_MODES)}")
        i = self.seat.instance(name)
        down = self._feeder_down(name, all_receivers=True)
        self.seat.set_upload(i, None if mode == "off" else mode)
        who = f"{self.seat.feeder_name(i)} (all its receivers)" if self.seat.radio_file(i) else name
        feeds = self._feeds(i)
        up = self._feeder_up(feeds[0]) if feeds else ""
        return self._with(f"{who}: rbn upload {mode}", down, up)

    def _radios_running(self) -> set[str]:
        return {r for i in self.seat.instances()
                if (r := self.seat.radio(i)) and self._running(i.name)}

    async def _reap(self) -> None:
        """A copy that stopped by itself (its radio lost for good, a crash) leaves a feeder with
        nothing to post, and a station whose radio has no copy left: stop them."""
        feeders = [name for role, name in list(self.procs) if role == "feeder"]
        if feeders and not self.busy:
            feeds = await asyncio.to_thread(self._feeding)
            for name in feeders:
                if self.busy:  # an operation began while this looked: it decides
                    break
                if not feeds.get(name):
                    log.info("Server: nothing feeds rbn feeder %s (its copies stopped by "
                             "themselves): stopping it", name)
                    await self.command("feeder", name, "stop", wait_s=5.0)
        stations = [name for role, name in list(self.procs) if role == "station"]
        if stations and not self.busy:  # a start or stop under way may be bringing its copy up
            running = await asyncio.to_thread(self._radios_running)
            for name in stations:
                if self.busy:
                    break
                if name not in running:
                    log.info("Server: no copy runs on radio %s: stopping its station", name)
                    await self.command("station", name, "stop", wait_s=5.0)

    def _feeding(self) -> dict[str, list[str]]:
        """Each feeder's running instances."""
        out: dict[str, list[str]] = {}
        for i in self.seat.instances():
            if self._running(i.name):
                out.setdefault(self.seat.feeder_name(i), []).append(i.name)
        return out

    def _live_band(self, name: str, band: str) -> str | None:
        """Ask the running copy to move to band in place; its answer, None to restart it, or
        SeatError while it recovers from a radio outage."""
        target = self._feed_target(name)
        if target is None:
            return None
        answer = feed_control(*target, {"band": band})
        if answer is None:
            log.warning("Server: %s gave no answer to a live band change: restarting it", name)
            return None
        ok, text = answer
        if not ok and text.startswith(RECOVERING):
            raise SeatError(f"{name}: {text}; nothing changed")  # a restart would wait the same way
        if not ok:
            log.info("Server: %s did not change band live (%s): restarting it", name, text)
            return None
        return text

    def _live_rx(self, name: str, rx: dict[str, Any]) -> tuple[bool, str] | None:
        """Ask the running copy to change its pan's receive path; (ok, its answer), or None when
        it gave none.  A refusal is reported, never followed by a restart: the radio keeps what it
        has."""
        target = self._feed_target(name)
        answer = feed_control(*target, {"rx": rx}) if target else None
        if answer is None:
            log.warning("Server: %s gave no answer to a receive path change", name)
        return answer

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
        with the copy's own join code, view only, then passes frames through unchanged both ways.
        Settings change only through the server's own commands (its lock and the config write)."""
        protocol = FEED_PROTOCOL if FEED_PROTOCOL in offered else None
        self._accept(writer, headers, protocol)
        if not ok:
            self._refuse(writer, ok)
            return
        if self.proxied >= MAX_PROXIED:
            self._refuse(writer, True, "too many connections")
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
        browser = str(writer.get_extra_info("peername")[0]).split("%")[0]  # no IPv6 scope
        up_w.write(feed_request(host, port, code, view=True, browser=browser))
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
        self.proxied += 1
        tasks = [
            asyncio.ensure_future(pipe(reader, up_w)),
            asyncio.ensure_future(pipe(up_r, writer)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.proxied -= 1
            for t in tasks:
                t.cancel()
            up_w.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(up_w.wait_closed(), 2)

    def _linked(self, st: dict[str, Any]) -> dict[str, Any]:
        """An instance's status with what its skimmer's link says: running, its feed port and
        receivers."""
        p = self.procs.get(("skimmer", st["name"]))
        st["linked"] = p is not None
        if p is not None:
            st["state"] = "running"
            st["pid"] = st["pid"] or p.pid
            for k in ("feed_port", "receivers", "cpu", "rss_mb"):
                st[k] = p.status.get(k)
        return st

    def _feed_target(self, name: str) -> tuple[str, int, str | None] | None:
        try:
            i = self.seat.instance(name)
        except SeatError:
            return None
        st = self._linked(self.seat.status(i))
        if st["state"] != "running" or not st["feed_port"]:
            return None
        bind = str((self.seat.config(i).get("gui_feed") or {}).get("bind", "127.0.0.1"))
        if bind not in ("0.0.0.0", "::", "") and not is_loopback(bind):
            log.warning("Server: %s's feed is bound to %s, not this machine only: not passed "
                        "through (its join code would cross the network)", name, bind)
            return None
        return "127.0.0.1", int(st["feed_port"]), st["feed_code"]


def feed_request(
    host: str, port: int, code: str | None, view: bool = False, browser: str | None = None
) -> bytes:
    """The opening request for a copy's feed on this machine, with the copy's own join code;
    view only (no control messages) for a browser's feed passed through, naming that browser."""
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    req = [
        f"GET /feed{'?' + VIEW_QUERY if view else ''}"
        f"{f'&{FOR_QUERY}={browser}' if view and browser else ''} HTTP/1.1",
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
