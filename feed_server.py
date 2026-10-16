"""WebSocket feed for SparkGap Web and the desktop console: windows, spots, status and spectrum.

WebSocket on the standard library (RFC 6455). The server runs its own asyncio loop in a daemon
thread, so live mode and file mode publish the same way: call the publish_* methods from any
thread. With no client connected, publishing only updates the connect snapshot.

Config block (off unless present):
    "gui_feed": {"bind": "127.0.0.1", "port": 7320, "join_code": null,
                 "tls_cert": null, "tls_key": null, "web": false}
A join code is required whenever bind is not a loopback address: 6 characters, case, spaces and
dashes ignored. A fresh one: python3 feed_server.py --new-code
"web": true also serves the browser console (web/) at http://<bind>:<port>/.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import numpy as np

log = logging.getLogger("sparkgap.feed")

PROTOCOL = "sparkgap.v1"
FEED_VERSION = 1
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PATH = "/feed"
CODE_PROTOCOL = "sparkgap.code."  # a browser offers its join code as this subprotocol + code
# browser clients learn of a refused code from these close codes (they cannot read the HTTP status)
CLOSE_WRONG_CODE, CLOSE_LOCKED_OUT, CLOSE_FULL = 4401, 4429, 4503
WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".ttf": "font/ttf",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

SNAPSHOT_WINDOW_S = 600  # decoded windows replayed to a client on connect
SNAPSHOT_SPOTS = 200
SIGNAL_TIMEOUT_S = 120  # a signal leaves the snapshot after this long without a window
MAX_HEADER_BYTES = 8_192
MAX_CLIENT_MESSAGE = 65_536
CLIENT_QUEUE_MAX = 5_000  # text messages queued for one client before it is dropped
MAX_CLIENTS = 16
MAX_PENDING = 32  # connections still sending their request; more are closed at once
TLS_HANDSHAKE = b"\x16"  # first byte of every TLS connection
PROBLEMS_KEPT = 5  # last warnings and errors shown on the status page
PROBLEM_CHARS = 200
CLOCK_CHECK_S = 60.0
SPECTRUM_QUEUE_MAX = 4  # spectrum rows queued per client; oldest dropped first
DEFAULT_SPECTRUM_HZ = 20.0
PING_INTERVAL_S = 20.0
IDLE_TIMEOUT_S = 60.0
COST_REPORT_S = 60.0

# join codes are minted without any pair confused by eye or over the air (O/0, I/1, S/5 ...)
CODE_MINT_ALPHABET = "ACDEFHJKMNPRTUVWXY3479"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # accepted on entry
CODE_LENGTH = 6
LOCKOUT_TRIES = 5  # wrong codes from one address ...
LOCKOUT_S = 600.0  # ... within this long refuse that address until they age out

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

# spectrum message: version, utc, first bin Hz, Hz per bin, dB per unit, bin count, then u8 per bin
SPECTRUM_HEADER = struct.Struct("<IddffH")


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch not in " -")


def valid_code(code: str) -> bool:
    c = normalize_code(code)
    return len(c) == CODE_LENGTH and all(ch in CODE_ALPHABET for ch in c)


def new_code() -> str:
    return "".join(secrets.choice(CODE_MINT_ALPHABET) for _ in range(CODE_LENGTH))


def show_code(code: str) -> str:
    c = normalize_code(code)
    return f"{c[:3]}-{c[3:]}"


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def encode_frame(opcode: int, payload: bytes) -> bytes:
    """Server frame: FIN set, unmasked."""
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65_536:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


def unmask(payload: bytes, mask: bytes) -> bytes:
    if not payload:
        return payload
    m = (mask * (len(payload) // 4 + 1))[: len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(m, "big")).to_bytes(len(payload), "big")


class ProtocolError(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    """One client frame: (fin, opcode, payload). Client frames must be masked."""
    b0, b1 = await reader.readexactly(2)
    if b0 & 0x70:
        raise ProtocolError(1002, "reserved bits set")
    if not b1 & 0x80:
        raise ProtocolError(1002, "client frame not masked")
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", await reader.readexactly(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", await reader.readexactly(8))
    opcode = b0 & 0x0F
    if opcode >= 0x8 and (n > 125 or not b0 & 0x80):
        raise ProtocolError(1002, "bad control frame")
    if n > MAX_CLIENT_MESSAGE:
        raise ProtocolError(1009, "message too big")
    mask = await reader.readexactly(4)
    return bool(b0 & 0x80), opcode, unmask(await reader.readexactly(n), mask)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(eq=False)
class Client:
    writer: asyncio.StreamWriter
    peer: str
    text: collections.deque[bytes] = field(default_factory=collections.deque)
    spectrum: collections.deque[bytes] = field(
        default_factory=lambda: collections.deque(maxlen=SPECTRUM_QUEUE_MAX)
    )
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    spectrum_hz: float = DEFAULT_SPECTRUM_HZ
    spectrum_credit: float = 0.0
    last_heard: float = field(default_factory=time.monotonic)
    since: float = field(default_factory=time.time)
    dropped_rows: int = 0
    closed: bool = False


def build_id() -> str | None:
    """The commit SparkGap runs from (git describe); None outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "describe", "--always", "--dirty"], cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


class FeedServer:
    def __init__(self, cfg: dict[str, Any], hello: dict[str, Any]) -> None:
        if not isinstance(cfg, dict):
            raise ValueError("gui_feed must be an object, e.g. {\"bind\": \"127.0.0.1\"}")
        self.bind = str(cfg.get("bind", "127.0.0.1"))
        port = cfg.get("port", 7320)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
            raise ValueError(f"gui_feed: port must be a whole number 0 to 65535, got {port!r}")
        self.port = port
        code = cfg.get("join_code")
        if code is not None and not valid_code(str(code)):
            raise ValueError(
                f"gui_feed: join_code must be {CODE_LENGTH} characters from {CODE_ALPHABET}"
            )
        self.code = normalize_code(str(code)) if code else None
        self.misses: dict[str, collections.deque[float]] = {}  # address -> times of wrong codes
        self.tls: ssl.SSLContext | None = None
        if cfg.get("tls_cert"):
            self.tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            self.tls.load_cert_chain(cfg["tls_cert"], cfg.get("tls_key"))
        if not is_loopback(self.bind) and not self.code:
            raise ValueError(f"gui_feed: bind {self.bind} is not loopback, a join_code is required")
        self.web = bool(cfg.get("web", False))
        self.hello = {"type": "hello", "v": FEED_VERSION, "host": socket.gethostname(),
                      "build": build_id(), **hello}
        self.clients: set[Client] = set()
        self.windows: collections.deque[dict[str, Any]] = collections.deque()
        self.spots: collections.deque[dict[str, Any]] = collections.deque(maxlen=SNAPSHOT_SPOTS)
        self.signals: dict[float, dict[str, Any]] = {}
        self.status: dict[str, Any] | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: Any = None
        self.started = threading.Event()
        self.thread: threading.Thread | None = None
        self.n_clients = 0  # read without the loop by the spectrum tap
        self.pending = 0  # connections in _handshake; touched only on the loop
        # status page counters, kept on the loop
        self.n_windows = 0
        self.calls: set[str] = set()
        self.spot_times: collections.deque[float] = collections.deque()
        self.bins_peak = 0
        self.problems = ProblemLog()
        self.proc: ProcessStats | None = None
        self.tap_cpu_s = 0.0  # CPU spent in SpectrumTap, all instances, for the cost log
        self.tid = 0

    # ---- lifecycle ----

    def start(self) -> None:
        logging.getLogger("sparkgap").addHandler(self.problems)  # all of SparkGap
        self.thread = threading.Thread(target=self._run, name="gui-feed", daemon=True)
        self.thread.start()
        self.started.wait(5)

    def _run(self) -> None:
        self.tid = threading.get_native_id()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.server = self.loop.run_until_complete(
                asyncio.start_server(self._handle, self.bind, self.port, ssl=self.tls)
            )
            self.port = self.server.sockets[0].getsockname()[1]
            log.info(
                "GUI feed on %s://%s:%d%s%s",
                "wss" if self.tls else "ws", self.bind, self.port, PATH,
                f", join code {show_code(self.code)}" if self.code else "",
            )
            if self.web:
                log.info("GUI feed: browser console at http%s://%s:%d/",
                         "s" if self.tls else "", self.bind, self.port)
        except OSError as e:
            log.error("GUI feed failed to start on %s:%d: %s", self.bind, self.port, e)
            self.started.set()
            return
        self.started.set()
        self.loop.create_task(self._keepalive())
        self.loop.create_task(self._report_cost())
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def stop(self) -> None:
        logging.getLogger("sparkgap").removeHandler(self.problems)
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._shutdown)
            if self.thread:
                self.thread.join(5)

    def _shutdown(self) -> None:
        assert self.loop is not None
        for c in list(self.clients):
            self._close(c, 1001, "server stopping")
        if self.server:
            self.server.close()
        self.loop.stop()

    # ---- publishing (any thread) ----

    def _call(self, fn: Callable[..., None], *args: Any) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(fn, *args)

    def publish_window(
        self, khz: float, text: str, wpm: int | None, snr: float | None, cost: float | None
    ) -> None:
        self._call(
            self._on_window,
            {
                "type": "window",
                "khz": round(float(khz), 2),  # float(): a numpy value would not serialise
                "text": text,
                "wpm": None if wpm is None else round(float(wpm), 1),
                "snr": None if snr is None else round(float(snr), 1),
                "cost": None if cost is None else round(float(cost), 2),
                "utc": time.time(),
            },
        )

    def publish_spot(self, khz: float, call: str, snr: float | None, wpm: int | None) -> None:
        self._call(
            self._on_text,
            {
                "type": "spot",
                "khz": round(float(khz), 2),
                "call": call,
                "snr": None if snr is None else float(snr),
                "wpm": float(wpm) if wpm else None,  # SparkGap's spots carry 0 for an unknown speed
                "utc": time.time(),
            },
            self.spots,
        )

    def publish_status(self, status: dict[str, Any]) -> None:
        self._call(self._on_status, {"type": "status", **status, "utc": time.time()})

    def publish_spectrum(
        self,
        first_bin_hz: float,
        hz_per_bin: float,
        db_per_unit: float,
        bins: bytes,
        utc: float | None = None,
    ) -> None:
        """utc: when the row's last sample arrived; rows from one IQ block share an arrival."""
        msg = (
            SPECTRUM_HEADER.pack(
                FEED_VERSION,
                time.time() if utc is None else utc,
                first_bin_hz,
                hz_per_bin,
                db_per_unit,
                len(bins),
            )
            + bins
        )
        self._call(self._on_spectrum, encode_frame(OP_BINARY, msg))

    # ---- loop side ----

    def _on_window(self, msg: dict[str, Any]) -> None:
        now = msg["utc"]
        self.n_windows += 1
        self.windows.append(msg)
        while self.windows and now - self.windows[0]["utc"] > SNAPSHOT_WINDOW_S:
            self.windows.popleft()
        self.signals[msg["khz"]] = {k: msg[k] for k in ("khz", "wpm", "snr", "utc")}
        for k in [k for k, s in self.signals.items() if now - s["utc"] > SIGNAL_TIMEOUT_S]:
            del self.signals[k]
        self._broadcast(msg)

    def _on_text(self, msg: dict[str, Any], keep: collections.deque[dict[str, Any]]) -> None:
        keep.append(msg)
        if msg["type"] == "spot":
            self.calls.add(msg["call"])
            self.spot_times.append(msg["utc"])
        self._broadcast(msg)

    def _on_status(self, msg: dict[str, Any]) -> None:
        """Adds what the feed itself counts: the status page's decoding, consoles and problems."""
        now = msg["utc"]
        while self.spot_times and now - self.spot_times[0] > 3600:
            self.spot_times.popleft()
        self.bins_peak = max(self.bins_peak, msg.get("bins") or 0)
        msg.update(
            windows=self.n_windows,
            calls=len(self.calls),
            spots_hour=len(self.spot_times),
            bins_peak=self.bins_peak,
            clients=[{"peer": c.peer, "since": round(c.since), "dropped": c.dropped_rows}
                     for c in self.clients],
            problems=self.problems.summary(),
        )
        self.status = msg
        self._broadcast(msg)

    def _on_spectrum(self, frame: bytes) -> None:
        for c in self.clients:
            # thin by count, not arrival time: rows from one IQ block arrive together
            c.spectrum_credit += c.spectrum_hz / SPECTRUM_ROWS_HZ
            if c.spectrum_credit < 1.0 - 1e-9:
                continue
            c.spectrum_credit = min(c.spectrum_credit - 1.0, 1.0)
            if len(c.spectrum) == c.spectrum.maxlen:
                c.dropped_rows += 1
            c.spectrum.append(frame)
            c.ready.set()

    def _broadcast(self, msg: dict[str, Any]) -> None:
        if not self.clients:
            return
        frame = encode_frame(OP_TEXT, json.dumps(msg, separators=(",", ":")).encode())
        for c in list(self.clients):
            self._queue(c, frame)

    def _queue(self, c: Client, frame: bytes) -> None:
        if len(c.text) >= CLIENT_QUEUE_MAX:
            log.warning("GUI feed: %s fell %d messages behind, dropped", c.peer, len(c.text))
            self._abort(c)
            return
        c.text.append(frame)
        c.ready.set()

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "signals": list(self.signals.values()),
            "windows": list(self.windows),
            "spots": list(self.spots),
            "status": self.status,
        }

    # ---- connection ----

    def _code_ok(self, host: str, given: str) -> bool | None:
        """True for the right code, False for a wrong one, None while host is locked out."""
        now = time.monotonic()
        for h in [h for h, m in self.misses.items() if now - m[-1] > LOCKOUT_S]:
            del self.misses[h]
        misses = self.misses.get(host, collections.deque())
        while misses and now - misses[0] > LOCKOUT_S:
            misses.popleft()
        if len(misses) >= LOCKOUT_TRIES:
            return None
        if not normalize_code(given):
            return False  # no code is not a guess: refused, not counted towards a lockout
        if hmac.compare_digest(normalize_code(given).encode(), str(self.code).encode()):
            return True
        misses.append(now)
        self.misses[host] = misses
        if len(misses) == LOCKOUT_TRIES:
            log.warning("GUI feed: %s locked out for %d min after %d wrong join codes",
                        host, LOCKOUT_S // 60, LOCKOUT_TRIES)
        return False

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool | None:
        """True: a client to serve. False: refused. None: a page served, or https turned away."""

        async def request() -> bytes | None:
            first = await reader.readexactly(1)
            if first == TLS_HANDSHAKE and self.tls is None:
                return None  # a browser trying https first: closing now lets it fall back to http
            return first + await reader.readuntil(b"\r\n\r\n")

        try:
            raw = await asyncio.wait_for(request(), 10)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            return False
        if raw is None:
            log.info("GUI feed: %s tried https; this feed is http (tls_cert not set)",
                     writer.get_extra_info("peername")[0])
            return None
        if len(raw) > MAX_HEADER_BYTES:
            return self._reject(writer, 431, "Request Header Fields Too Large")
        lines = raw.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if len(parts) != 3 or parts[0] != "GET":
            return self._reject(writer, 405, "Method Not Allowed")
        path = parts[1].split("?")[0]
        if path != PATH:
            if self.web and headers.get("upgrade", "").lower() != "websocket":
                return self._serve_file(writer, path)
            return self._reject(writer, 404, "Not Found")
        if (
            headers.get("upgrade", "").lower() != "websocket"
            or "upgrade" not in headers.get("connection", "").lower()
            or headers.get("sec-websocket-version") != "13"
            or "sec-websocket-key" not in headers
        ):
            return self._reject(writer, 400, "Bad Request")
        offered = [
            p.strip() for p in headers.get("sec-websocket-protocol", "").split(",") if p.strip()
        ]
        # without a join code only this machine can connect; a browser on it must be on our page,
        # or any web site it visits could read the feed. Host is the requester's to set (DNS
        # rebinding), so the origin itself must be a loopback address on our port.
        origin = headers.get("origin")
        if not self.code and origin is not None and not self._local_origin(origin):
            return self._reject(writer, 403, "Forbidden")
        # a browser cannot set headers or read an HTTP refusal: it offers the code as a
        # subprotocol and learns of a refusal from the close code
        browser = origin is not None
        refused = 0
        if self.code:
            auth = headers.get("authorization", "")
            given = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not given:
                given = next(
                    (p[len(CODE_PROTOCOL) :] for p in offered if p.startswith(CODE_PROTOCOL)), ""
                )
            ok = self._code_ok(str(writer.get_extra_info("peername")[0]), given)
            if not browser and ok is None:
                return self._reject(writer, 429, "Too Many Requests")
            if not browser and not ok:
                return self._reject(writer, 401, "Unauthorized")
            refused = CLOSE_LOCKED_OUT if ok is None else 0 if ok else CLOSE_WRONG_CODE
        if len(self.clients) >= MAX_CLIENTS:
            if not browser:
                return self._reject(writer, 503, "Service Unavailable")
            refused = refused or CLOSE_FULL
        reply = [
            "HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Accept: {accept_key(headers['sec-websocket-key'])}",
        ]
        if PROTOCOL in offered:
            reply.append(f"Sec-WebSocket-Protocol: {PROTOCOL}")
        writer.write(("\r\n".join(reply) + "\r\n\r\n").encode())
        if refused:
            # a browser sees only the close code: accept the upgrade, then close with the reason
            reason = {CLOSE_WRONG_CODE: "wrong join code", CLOSE_LOCKED_OUT: "locked out",
                      CLOSE_FULL: "too many clients"}[refused]
            writer.write(encode_frame(OP_CLOSE, struct.pack("!H", refused) + reason.encode()))
            await writer.drain()
            return False
        await writer.drain()
        return True

    def _local_origin(self, origin: str) -> bool:
        try:
            u = urlsplit(origin)
            local = is_loopback(u.hostname or "") and u.port == self.port
            return u.scheme in ("http", "https") and local
        except ValueError:
            return False

    @staticmethod
    def _serve_file(writer: asyncio.StreamWriter, path: str) -> bool | None:
        """The browser console: web/ and its fonts and icons folders, nothing else."""
        name = path.lstrip("/") or "index.html"
        folder, _, leaf = name.rpartition("/")
        base = {"": WEB_DIR, "fonts": WEB_DIR / "fonts", "icons": WEB_DIR / "icons"}.get(folder)
        if base is None:
            return FeedServer._reject(writer, 404, "Not Found")
        f = base / leaf
        if (
            not leaf
            or leaf.startswith(".")
            or "\\" in leaf
            or f.suffix not in WEB_TYPES
            or not f.is_file()
            or not f.resolve().is_relative_to(base.resolve())  # no symlink out of the folder
        ):
            return FeedServer._reject(writer, 404, "Not Found")
        try:
            body = f.read_bytes()
        except OSError:
            return FeedServer._reject(writer, 404, "Not Found")
        head = [
            "HTTP/1.1 200 OK",
            f"Content-Type: {WEB_TYPES[f.suffix]}",
            f"Content-Length: {len(body)}",
            "Cache-Control: no-cache",
            "X-Content-Type-Options: nosniff",
            "Content-Security-Policy: default-src 'self'; connect-src 'self' ws: wss:; "
            "style-src 'self' 'unsafe-inline'",
            "Connection: close",
        ]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + body)
        return None

    @staticmethod
    def _reject(writer: asyncio.StreamWriter, code: int, reason: str) -> bool:
        writer.write(
            f"HTTP/1.1 {code} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        return False

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        host, port = writer.get_extra_info("peername")[:2]
        peer = f"{host}:{port}"
        if self.pending >= MAX_PENDING:
            writer.close()
            return
        self.pending += 1
        try:
            accepted = await self._handshake(reader, writer)
        finally:
            self.pending -= 1
        if not accepted:
            if accepted is False:
                log.info("GUI feed: refused %s", peer)
            writer.close()
            return
        c = Client(writer, peer)
        for msg in (self.hello, self.snapshot()):
            c.text.append(encode_frame(OP_TEXT, json.dumps(msg, separators=(",", ":")).encode()))
        c.ready.set()
        self.clients.add(c)
        self.n_clients = len(self.clients)
        log.info("GUI feed: %s connected (%d clients)", peer, self.n_clients)
        sender = asyncio.ensure_future(self._send(c))
        try:
            await self._receive(c, reader)
        except ProtocolError as e:
            self._close(c, e.code, str(e))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            c.closed = True
            c.ready.set()
            self.clients.discard(c)
            self.n_clients = len(self.clients)
            await asyncio.gather(sender, return_exceptions=True)
            writer.close()
            log.info(
                "GUI feed: %s left (%d clients, %d spectrum rows dropped)",
                peer,
                self.n_clients,
                c.dropped_rows,
            )

    async def _receive(self, c: Client, reader: asyncio.StreamReader) -> None:
        parts: list[bytes] = []
        fragmented = False  # inside a text message sent in fragments
        while not c.closed:
            fin, op, data = await read_frame(reader)
            c.last_heard = time.monotonic()
            if (op == OP_CONT) != fragmented and op in (OP_TEXT, OP_CONT):
                raise ProtocolError(1002, "bad fragment sequence")
            if op == OP_PING:
                self._queue(c, encode_frame(OP_PONG, data))
            elif op == OP_PONG:
                pass
            elif op == OP_CLOSE:
                self._close(c, 1000, "")
                return
            elif op in (OP_TEXT, OP_CONT):
                parts.append(data)
                if sum(len(p) for p in parts) > MAX_CLIENT_MESSAGE:
                    raise ProtocolError(1009, "message too big")
                fragmented = not fin
                if fin:
                    self._on_client_message(c, b"".join(parts))
                    parts = []
            else:
                raise ProtocolError(1003, "unsupported message type")

    def _on_client_message(self, c: Client, data: bytes) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if isinstance(msg, dict) and msg.get("type") == "subscribe":
            hz = msg.get("spectrum_hz", DEFAULT_SPECTRUM_HZ)
            if isinstance(hz, (int, float)):
                c.spectrum_hz = max(0.5, min(SPECTRUM_ROWS_HZ, float(hz)))

    async def _send(self, c: Client) -> None:
        try:
            while True:
                await c.ready.wait()
                c.ready.clear()
                while c.text or c.spectrum:
                    c.writer.write(c.text.popleft() if c.text else c.spectrum.popleft())
                    await c.writer.drain()
                if c.closed:
                    c.writer.close()
                    return
        except (ConnectionError, OSError):
            c.closed = True

    def _close(self, c: Client, code: int, reason: str) -> None:
        if c.closed:
            return
        c.text.append(encode_frame(OP_CLOSE, struct.pack("!H", code) + reason.encode()[:120]))
        c.spectrum.clear()
        c.closed = True
        c.ready.set()

    def _abort(self, c: Client) -> None:
        """Cut a stalled client off: a close frame it never reads would hold its sender."""
        c.closed = True
        c.text.clear()
        c.spectrum.clear()
        c.ready.set()
        c.writer.transport.abort()

    async def _report_cost(self) -> None:
        """Log the feed's own CPU once a minute: spectrum tap (SparkGap's loop) and this thread."""

        def cpu(path: str) -> float:
            try:
                with open(path) as fh:
                    f = fh.read().rsplit(")", 1)[1].split()
                return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
            except (OSError, ValueError, IndexError):
                return 0.0

        me, proc = f"/proc/self/task/{self.tid}/stat", "/proc/self/stat"
        last = (time.monotonic(), self.tap_cpu_s, cpu(me), cpu(proc))
        while True:
            await asyncio.sleep(COST_REPORT_S)
            now = (time.monotonic(), self.tap_cpu_s, cpu(me), cpu(proc))
            dt = now[0] - last[0]
            tap, sender, total = ((now[i] - last[i]) * 100 / dt for i in (1, 2, 3))
            log.info(
                "GUI feed cost: tap %.1f%%, sender thread %.1f%%, SparkGap total %.1f%% of a core, "
                "%d clients",
                tap,
                sender,
                total,
                self.n_clients,
            )
            last = now

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            now = time.monotonic()
            for c in list(self.clients):
                if now - c.last_heard > IDLE_TIMEOUT_S:
                    self._abort(c)
                else:
                    self._queue(c, encode_frame(OP_PING, b""))


SPECTRUM_RES_HZ = 50.0  # target FFT resolution
SPECTRUM_MAX_BINS = 2_048
SPECTRUM_ROWS_HZ = 60.0  # rows produced; each client is thinned to its own rate
DB_PER_UNIT = 0.25  # u8 steps: 0.25 dB, 0 .. 63.75 dB over the noise floor
# noise floor per block, so the radio's IQ roll-off at the edges is not drawn black
FLOOR_BLOCK_HZ = 1_500.0
FLOOR_SMOOTH = 0.1  # per row: block floors settle over ~10 rows (1/6 s) instead of flickering


class SpectrumTap:
    """Turns the IQ fed to the scanner into spectrum rows for the feed. Idle without clients."""

    def __init__(self, feed: FeedServer, sample_rate: int, center_hz: float) -> None:
        self.feed = feed
        self.sample_rate = sample_rate
        self.center_hz = center_hz
        self.nfft = 1 << int(np.ceil(np.log2(sample_rate / SPECTRUM_RES_HZ)))
        self.pool = max(1, self.nfft // SPECTRUM_MAX_BINS)
        self.hop = int(sample_rate / SPECTRUM_ROWS_HZ)
        self.window = np.hanning(self.nfft).astype(np.float32)
        self.buf = np.zeros(self.nfft, dtype=np.complex64)
        self.since = 0
        n_out = self.nfft // self.pool
        self.floor_blk = max(8, round(FLOOR_BLOCK_HZ / (sample_rate / n_out)))
        self.floor_x = (np.arange(n_out // self.floor_blk) + 0.5) * self.floor_blk
        self.floor: np.ndarray | None = None

    def feed_iq(self, i_samples: Any, q_samples: Any) -> None:
        if not self.feed.n_clients:
            self.since = 0
            return
        t0 = time.thread_time()
        try:
            self._feed_iq(i_samples, q_samples)
        finally:
            self.feed.tap_cpu_s += time.thread_time() - t0

    def _feed_iq(self, i_samples: Any, q_samples: Any) -> None:
        x = np.asarray(i_samples, dtype=np.float32) + 1j * np.asarray(q_samples, dtype=np.float32)
        hist = np.concatenate((self.buf, x.astype(np.complex64)))
        # one row per hop boundary inside this block, each over the nfft samples ending there
        end = self.nfft + self.hop - self.since
        now = time.time()
        while end <= len(hist):
            self.row(hist[end - self.nfft : end], now - (len(hist) - end) / self.sample_rate)
            end += self.hop
        self.since = (self.since + len(x)) % self.hop
        self.buf = hist[-self.nfft :]

    def row(self, seg: np.ndarray, utc: float) -> None:
        p = np.abs(np.fft.fftshift(np.fft.fft(seg * self.window))) ** 2
        if self.pool > 1:
            p = p.reshape(-1, self.pool).max(axis=1)
        db = 10 * np.log10(p + 1e-20)
        nb = len(self.floor_x)
        med = np.median(db[: nb * self.floor_blk].reshape(nb, self.floor_blk), axis=1)
        self.floor = med if self.floor is None else self.floor + FLOOR_SMOOTH * (med - self.floor)
        over = (db - np.interp(np.arange(len(db)), self.floor_x, self.floor)) / DB_PER_UNIT
        bins = np.clip(over, 0, 255).astype(np.uint8).tobytes()
        hz_per_bin = self.sample_rate / self.nfft * self.pool
        self.feed.publish_spectrum(
            self.center_hz - self.sample_rate / 2, hz_per_bin, DB_PER_UNIT, bins, utc
        )


class ProblemLog(logging.Handler):
    """Counts SparkGap's warnings and errors and keeps the last few, for the status page."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.warnings = 0
        self.errors = 0
        self.last: collections.deque[dict[str, Any]] = collections.deque(maxlen=PROBLEMS_KEPT)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            self.errors += 1
        else:
            self.warnings += 1
        try:
            text = record.getMessage()[:PROBLEM_CHARS]
        except Exception:  # a bad format string must not break logging
            text = str(record.msg)[:PROBLEM_CHARS]
        self.last.append({"utc": round(record.created), "level": record.levelname, "msg": text})

    def summary(self) -> dict[str, Any]:
        return {"warnings": self.warnings, "errors": self.errors, "last": list(self.last)}


def log_file() -> str | None:
    """Where SparkGap's log goes when that is a file (output redirected), else None."""
    for fd in (2, 1):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith("/") and os.path.isfile(target):
            return target
    name = getattr(sys.stderr, "name", "")
    return name if name and not name.startswith("<") and os.path.isfile(name) else None


def clock_synced() -> bool | None:
    """Whether the system clock is NTP-synchronised; None where that cannot be read."""
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return {"yes": True, "no": False}.get(out)


class ProcessStats:
    """CPU % since the previous call, resident memory and clock sync of this process's machine.
    The clock is read on a thread of its own: timedatectl may be slow, and sample() runs in
    SparkGap's main loop."""

    def __init__(self) -> None:
        self.last = (time.monotonic(), sum(os.times()[:2]))
        self.clock: bool | None = None
        self.clock_thread: threading.Thread | None = None

    def _watch_clock(self) -> None:
        while True:
            self.clock = clock_synced()
            time.sleep(CLOCK_CHECK_S)

    def sample(self) -> dict[str, Any]:
        now, cpu = time.monotonic(), sum(os.times()[:2])
        dt = now - self.last[0]
        pct = round(100 * (cpu - self.last[1]) / dt) if dt > 0 else None
        self.last = (now, cpu)
        rss_mb = None
        try:
            with open("/proc/self/statm") as fh:
                rss_mb = int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") // 1_048_576
        except (OSError, ValueError, AttributeError):
            pass
        if self.clock_thread is None:
            self.clock_thread = threading.Thread(target=self._watch_clock, name="gui-clock",
                                                 daemon=True)
            self.clock_thread.start()
        return {"cpu": pct, "rss_mb": rss_mb, "clock_synced": self.clock}


if __name__ == "__main__":
    if sys.argv[1:] == ["--new-code"]:
        print(show_code(new_code()))
    else:
        sys.exit("usage: python3 feed_server.py --new-code")
