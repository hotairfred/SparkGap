"""A process the SparkGap server runs (a copy, its radio's station, an RBN feeder) dials the
server's /process hub on this machine: it says who it is, sends its status and takes commands.

The link is the server's view of the process: connected means running, a closed link means it
stopped. A link that cannot reach the server keeps trying; the process runs the same without it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import socket
import struct
import threading
from collections.abc import Callable
from typing import Any, BinaryIO

PROCESS_PATH = "/process"
PROCESS_PROTOCOL = "sparkgap.process.v1"
SERVER_PORT = 7320  # the server's port; SPARKGAP_SERVER_PORT overrides it
RECONNECT_S = 5.0  # between tries while the server is away
CONNECT_S = 5.0
LOG_LINE_MAX = 400  # a forwarded log line is cut here
OP_TEXT, OP_CLOSE, OP_PING, OP_PONG = 0x1, 0x8, 0x9, 0xA

log = logging.getLogger("sparkgap.link")

# a command from the server: (cmd, args) -> (ok, message)
Command = Callable[[str, dict[str, Any]], tuple[bool, str]]


def client_frame(opcode: int, payload: bytes) -> bytes:
    """One masked client frame, FIN set."""
    mask = secrets.token_bytes(4)
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
    elif n < 65_536:
        head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
    return head + mask + bytes(b ^ mask[k % 4] for k, b in enumerate(payload))


def read_frame(f: BinaryIO) -> tuple[int, bytes]:
    """One unmasked server frame: (opcode, payload). EOFError when the server closes."""

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


class _Forward(logging.Handler):
    """The process's warnings and errors, to the server's log (dropped while it is away)."""

    def __init__(self, link: ProcessLink) -> None:
        super().__init__(logging.WARNING)
        self.link = link

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        self.link._send({"type": "log", "level": record.levelname, "msg": msg[:LOG_LINE_MAX]})


class ProcessLink:
    """This process's link to the server: start() it once; status() sends what changed."""

    def __init__(
        self,
        role: str,
        name: str,
        on_command: Command | None = None,
        port: int | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        self.role, self.name, self.on_command, self.host = role, name, on_command, host
        self.port = port or int(os.environ.get("SPARKGAP_SERVER_PORT") or SERVER_PORT)
        self.connected = threading.Event()
        self._status: dict[str, Any] = {}
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._forward: _Forward | None = None

    def start(self) -> ProcessLink:
        threading.Thread(target=self._run, name=f"link-{self.role}", daemon=True).start()
        self._forward = _Forward(self)
        logging.getLogger().addHandler(self._forward)
        return self

    def close(self) -> None:
        self._stop.set()
        if self._forward is not None:
            logging.getLogger().removeHandler(self._forward)
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.sendall(client_frame(OP_CLOSE, b""))
                except OSError:
                    pass
                self._sock.close()
                self._sock = None

    def status(self, **fields: Any) -> None:
        """Update this process's status; the server gets it now, or on the next connect."""
        self._status.update(fields)
        self._send({**self._status, "type": "status"})  # a field named type never hides the message

    def _send(self, msg: dict[str, Any]) -> None:
        with self._lock:
            if self._sock is None:
                return
            try:
                self._sock.sendall(client_frame(OP_TEXT, json.dumps(msg).encode()))
            except OSError:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as e:  # whatever goes wrong, the link retries; the process runs on
                log.debug("link to the server: %s", e)
            with self._lock:
                self._sock = None
            if self.connected.is_set():
                log.info("Link: the server went away; trying again every %.0f s", RECONNECT_S)
            self.connected.clear()
            self._stop.wait(RECONNECT_S)

    def _session(self) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = (
            f"GET {PROCESS_PATH} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Protocol: {PROCESS_PROTOCOL}\r\n\r\n"
        )
        with socket.create_connection((self.host, self.port), timeout=CONNECT_S) as s:
            s.sendall(req.encode())
            f = s.makefile("rb")
            if not f.readline().startswith(b"HTTP/1.1 101"):
                raise ValueError("the server refused the link")
            while f.readline() not in (b"\r\n", b""):
                pass
            s.settimeout(None)  # the server pings; a dead one closes the socket
            with self._lock:
                self._sock = s
            hello = {"type": "hello", "role": self.role, "name": self.name,
                     "pid": os.getpid(), "pgid": os.getpgid(0)}
            self._send(hello)
            if self._status:
                self._send({**self._status, "type": "status"})
            self.connected.set()
            log.info("Link: %s %s registered with the server on port %d", self.role, self.name,
                     self.port)
            while not self._stop.is_set():
                op, data = read_frame(f)
                if op == OP_PING:
                    with self._lock:
                        s.sendall(client_frame(OP_PONG, data))
                elif op == OP_CLOSE:
                    return
                elif op == OP_TEXT:
                    self._command(json.loads(data))

    def _command(self, msg: dict[str, Any]) -> None:
        if msg.get("type") != "command":
            return
        cmd, args = str(msg.get("cmd")), msg.get("args") or {}
        try:
            ok, text = self.on_command(cmd, args) if self.on_command else (False, "no commands")
        except Exception as e:  # a bad command must not take the link down
            ok, text = False, f"{cmd}: {e}"
        self._send({"type": "result", "id": msg.get("id"), "ok": ok,
                    ("message" if ok else "error"): text})
