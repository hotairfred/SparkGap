#!/usr/bin/env python3
"""sparkgap_ctl.py: the command line client of this machine's SparkGap server (cdub89/SparkGap#33).

Every start, stop and change goes through the server, as SparkGap Web's do: one controller per
machine.  It reads the server's port and join code from <root>/server.json.

usage: sparkgap_ctl.py [--root R] status [name ...]
       sparkgap_ctl.py [--root R] start|stop|restart [name ...]      (no name: every instance)
       sparkgap_ctl.py [--root R] band <name> <band> | rec <name> on|off | rbn <name> off|dry|live
       sparkgap_ctl.py [--root R] set <name> key=value ...           (values as JSON, else text)
       sparkgap_ctl.py [--root R] discover [flex|hpsdr] [address]      (no type: as sparkgap.sh setup)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import ssl
import sys
import time
from pathlib import Path
from typing import Any

from process_link import OP_CLOSE, OP_PING, OP_PONG, OP_TEXT, client_frame, read_frame

SERVER_PATH = "/server"
SERVER_PROTOCOL = "sparkgap.server.v1"
CONNECT_S = 5.0
OP_WAIT_S = 240.0  # a start waits for the copy, its station and its feeder
EXIT_FAILED, EXIT_NO_SERVER = 1, 2
TOP_CALLS = 10


class NoServer(RuntimeError):
    pass


def default_root() -> Path:
    return Path(os.environ.get("SPARKGAP_ROOT") or Path.home() / "sparkgap")


def server_settings(root: Path) -> tuple[int, str, bool]:
    """The server's port, join code and whether it serves TLS, from server.json."""
    try:
        cfg = json.loads((root / "server.json").read_text())
        return int(cfg.get("port", 7320)), str(cfg["join_code"]), bool(cfg.get("tls_cert"))
    except (OSError, ValueError, KeyError) as e:
        raise NoServer(f"{root}/server.json: {e} (written at the server's first start)") from None


class Client:
    """One connection to the server's /server WebSocket."""

    def __init__(self, port: int, code: str, host: str = "127.0.0.1", tls: bool = False) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        try:
            self.sock = socket.create_connection((host, port), timeout=CONNECT_S)
            if tls:  # this machine's own server: its certificate names its public address
                ctx = ssl.create_default_context()
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
                self.sock = ctx.wrap_socket(self.sock)
        except OSError as e:
            raise NoServer(f"no SparkGap server on {host}:{port} ({e})") from None
        self.sock.sendall((
            f"GET {SERVER_PATH} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Protocol: {SERVER_PROTOCOL}\r\nAuthorization: Bearer {code}\r\n\r\n"
        ).encode())
        self.f = self.sock.makefile("rb")
        if not self.f.readline().startswith(b"HTTP/1.1 101"):
            raise NoServer(f"the server on {host}:{port} refused the connection")
        while self.f.readline() not in (b"\r\n", b""):
            pass
        self.next_id = 1

    def close(self) -> None:
        try:
            self.sock.sendall(client_frame(OP_CLOSE, b""))
        except OSError:
            pass
        self.sock.close()

    def messages(self, wait_s: float) -> Any:
        """The server's JSON messages until wait_s passes; pings answered."""
        end = time.monotonic() + wait_s
        while (left := end - time.monotonic()) > 0:
            self.sock.settimeout(left)
            try:
                op, data = read_frame(self.f)
            except TimeoutError:
                return
            except (EOFError, OSError):
                raise NoServer("the server closed the connection") from None
            if op == OP_PING:
                self.sock.sendall(client_frame(OP_PONG, data))
            elif op == OP_CLOSE:
                raise NoServer("the server closed the connection (wrong join code?)")
            elif op == OP_TEXT:
                yield json.loads(data)

    def snapshot(self) -> dict[str, Any]:
        for msg in self.messages(CONNECT_S + 5):
            if msg.get("type") == "seat":
                return msg
        raise NoServer("the server sent no state")

    def request(self, cmd: str, name: str | None = None,
                settings: dict[str, Any] | None = None) -> tuple[bool, str]:
        rid, self.next_id = self.next_id, self.next_id + 1
        msg: dict[str, Any] = {"type": "server", "id": rid, "cmd": cmd, "by": "sparkgap.sh"}
        if name is not None:
            msg["name"] = name
        if settings is not None:
            msg["settings"] = settings
        self.sock.sendall(client_frame(OP_TEXT, json.dumps(msg).encode()))
        for m in self.messages(OP_WAIT_S):
            if m.get("type") == "server_result" and m.get("id") == rid:
                return bool(m.get("ok")), str(m.get("message") or m.get("error") or "")
        return False, f"{cmd} {name or ''}: no answer from the server in {OP_WAIT_S:.0f} s"


def _uptime(started: float | None, now: float) -> str:
    if not started:
        return ""
    s = int(now - started)
    return f"up {s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"up {s // 60}m {s % 60}s"


def receiver_lines(i: dict[str, Any], now: float, rr: dict[str, Any] | None = None) -> list[str]:
    """One receiver: its state line, then its detail; rr, one receiver of an HPSDR radio (the
    detail is its radio's process, shared by all its receivers)."""
    state = i["state"] + (f" ({i['busy']})" if i.get("busy") else "")
    rx = i.get("receivers") or []
    where = ", ".join(f"{r['band']} {r['centre_khz']:.1f}" for r in rx) or i.get("band") or "?"
    if rr is not None:
        where = rr["band"] + (f" {rr['centre_khz']:.1f}" if rr.get("centre_khz") else "")
        where += f", spots {rr['spots']}" if rr.get("spots") is not None else ""
        where += f", process {i['name']} (all {len(i['receiver_list'])} receivers)"
    head = [rr["name"] if rr else i["name"], state, where, i.get("build") or ""]
    if i["state"] == "running":
        head += [_uptime(i.get("started"), now)]
        if i.get("cpu") is not None:
            head += [f"cpu {i['cpu']}%"]
        if i.get("rss_mb") is not None:
            head += [f"{i['rss_mb']} MB"]
    out = ["  " + ", ".join(x for x in head if x)]
    out.append(f"    spots {i.get('spots', 0)}, errors {i.get('errors', 0)}, warnings "
               f"{i.get('warnings', 0)}, reconnects {i.get('reconnects', 0)}, telnet "
               f"{i.get('telnet_port')}, feed {i.get('feed_port') or 'off'}")
    if i.get("top_calls"):
        out.append("    top calls: " + " ".join(f"{c}({n})" for c, n in i["top_calls"][:TOP_CALLS]))
    if i.get("last_error"):
        out.append(f"    last error: {i['last_error']}")
    if i.get("log"):
        out.append(f"    log {i['log']}")
    return out


class Paint:
    """Terminal highlighting for the few things worth seeing at a glance; plain when off."""

    def __init__(self, on: bool) -> None:
        self.on = on

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on and text else text

    def state(self, state: str) -> str:
        return self(state, {"running": "32", "stopped": "31"}.get(state, "33"))

    def upload(self, mode: str) -> str:
        return self(mode.upper(), {"live": "1;32", "dry": "33"}.get(mode, "2"))

    def count(self, n: int, code: str) -> str:
        return self(str(n), code) if n else str(n)


COLUMNS = ("receiver", "state", "band", "kHz", "spots", "err", "warn", "up", "cpu", "mem", "build")


def table(rows: list[list[str]], shown: list[list[str]]) -> list[str]:
    """Rows padded to their plain width (shown: the same cells with highlighting)."""
    widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]))]
    out = []
    for r, s in zip(rows, shown, strict=True):
        cells = (cell + " " * (w - len(plain)) for cell, plain, w in zip(s, r, widths, strict=True))
        out.append(("   " + "  ".join(cells)).rstrip())
    return out


def receiver_row(i: dict[str, Any], now: float, paint: Paint) -> tuple[list[str], list[str]]:
    rx = i.get("receivers") or []
    band = ", ".join(r["band"] for r in rx) or str(i.get("band") or "?")
    khz = ", ".join(f"{r['centre_khz']:.1f}" for r in rx) if i["state"] == "running" else ""
    state = i["state"] + (f" ({i['busy']})" if i.get("busy") else "")
    up = _uptime(i.get("started"), now).removeprefix("up ") if i["state"] == "running" else ""
    cpu = f"{i['cpu']}%" if i.get("cpu") is not None and up else ""
    mem = f"{i['rss_mb']} MB" if i.get("rss_mb") is not None and up else ""
    err, warn = int(i.get("errors") or 0), int(i.get("warnings") or 0)
    plain = [i["name"], state, band, khz, str(i.get("spots", 0)), str(err), str(warn), up, cpu,
             mem, str(i.get("build") or "")]
    rec = i["state"] == "running" and not i.get("busy") and (i.get("settings") or {}).get("record")
    shown_state = paint.state(state) if not i.get("busy") else paint(state, "33")
    if rec:  # recording IQ: said beside the state, as the web's receiver card does
        plain[1] += ", rec"
        shown_state += ", " + paint("rec", "33")
    shown = [paint(i["name"], "1"), shown_state,
             *plain[2:5], paint.count(err, "31"), paint.count(warn, "33"), *plain[7:]]
    return plain, shown


def receiver_block(mine: list[dict[str, Any]], now: float, paint: Paint) -> list[str]:
    if mine and all(i.get("receiver_list") for i in mine):  # an HPSDR radio: its process is the skimmer row
        head = [*COLUMNS[:5], "calls", "windows", "bins"]
        cells = [head, *([rr["name"], receiver_row(i, now, paint)[0][1], rr["band"],
                          f"{rr['centre_khz']:.1f}" if rr.get("centre_khz") and i["state"] == "running" else "",
                          *(_cell(rr.get(k)) for k in ("spots", "calls", "windows")), _cell(_bins(i, rr))]
                         for i in mine for rr in i["receiver_list"])]
        return table(cells, [[paint(h, "34") for h in head],
                             *([paint(c[0], "1"), paint.state(c[1]), *c[2:]] for c in cells[1:])])
    head: list[str] = list(COLUMNS)
    rows, shown = [head], [[paint(h, "34") for h in head]]
    owners: list[dict[str, Any] | None] = [None]
    for i in mine:
        r, s = receiver_row(i, now, paint)
        for n, rr in enumerate(i.get("receiver_list") or [None]):
            if rr is None:
                rows.append(r)
                shown.append(s)
            else:  # the process's state on each; its spots per band; up, cpu, mem and build once
                spots = "" if rr.get("spots") is None else str(rr["spots"])
                khz = f"{rr['centre_khz']:.1f}" if rr.get("centre_khz") and i["state"] == "running" else ""
                rest = r[5:] if n == 0 else ["", "", "", "", "", ""]
                rows.append([rr["name"], r[1], rr["band"], khz, spots, *rest])
                shown.append([paint(rr["name"], "1"), s[1], rr["band"], khz, spots,
                              *(s[5:] if n == 0 else rest)])
            owners.append(i if n == 0 else None)
    out = []
    for line, i in zip(table(rows, shown), owners, strict=True):
        out.append(line)
        if i and i["state"] != "running" and i.get("last_error"):
            out.append("     " + paint(f"last error: {i['last_error']}", "31"))
    return out


LABEL_W = 8  # "station " : the labels under a radio's bar line up


def label(paint: Paint, text: str) -> str:
    return "   " + paint(text, "34") + " " * (LABEL_W - len(text) + 1)


def discover_settings(args: list[str]) -> dict[str, str]:
    """discover's words, in any order: an address (IPv4 or IPv6), a radio type, and name=<name>
    for an HPSDR radio not set up yet."""
    return {("name" if x.startswith("name=") else "address" if "." in x or ":" in x else "type"):
            x.removeprefix("name=") for x in args}


def _cell(v: Any) -> str:
    return "" if v is None else str(v)


def _bins(i: dict[str, Any], rr: dict[str, Any]) -> int | None:
    """An HPSDR receiver's live bin count, as its process last sent it; None when stopped."""
    if i["state"] != "running":
        return None
    return next((x.get("bins") for x in i.get("receivers") or [] if x.get("rx") == rr["receiver"] - 1), None)


def _total(i: dict[str, Any], key: str) -> int | None:
    """An HPSDR radio's total over its receivers, as SparkGap Web's tiles show it."""
    vals = [(_bins(i, rr) if key == "bins" else rr.get(key)) for rr in i.get("receiver_list") or []]
    return sum(v for v in vals if v is not None) if any(v is not None for v in vals) else None


def skimmer_text(i: dict[str, Any], now: float, paint: Paint) -> str:
    """An HPSDR radio's one process, for all its receivers: state, up, cpu, memory, errors,
    warnings and build."""
    state = i["state"] + (f" ({i['busy']})" if i.get("busy") else "")
    run = i["state"] == "running"
    err, warn = int(i.get("errors") or 0), int(i.get("warnings") or 0)
    parts = [paint.state(state), _uptime(i.get("started"), now) if run else "",
             f"cpu {i['cpu']}%" if run and i.get("cpu") is not None else "",
             f"{i['rss_mb']} MB" if run and i.get("rss_mb") is not None else "",
             *(f"{k} {_total(i, k)}" for k in ("spots", "calls", "windows", "bins") if _total(i, k) is not None),
             f"err {paint.count(err, '31')}", f"warn {paint.count(warn, '33')}", str(i.get("build") or "")]
    return "  ".join(x for x in parts if x)


def radio_lines(r: dict[str, Any], station: dict[str, Any] | None, n: int,
                paint: Paint, skimmer: str = "") -> list[str]:
    """The radio's bar (connected or not, its nickname, model, address, SCUs, pans in use),
    then its node, its skimmer (an HPSDR radio's one process) and feeder, one labelled row each."""
    facts = [r.get("model") or r.get("type") or "", r.get("address") or ""]
    if r.get("num_scu"):
        facts.append(f"{r['num_scu']} SCU")
    if r.get("max_panadapters"):
        facts.append(f"pans {n}/{r['max_panadapters']}")
    if r.get("board_id"):
        facts.append(f"board {r['board_id']}")
    if r.get("receivers"):
        facts.append(f"{r['receivers']} receivers")
    link = connection(station) or (("Running" if n else "Stopped") if r.get("type") == "hpsdr" else "")
    shown = paint(link, {"Connected": "32", "Not connected": "31", "Running": "32",
                         "Stopped": "2"}.get(link, "33")) if link else ""
    bar = f"{paint('━━ Radio:', '1')} {shown + ' ' if shown else ''}{paint(r['name'] + ' ━━', '1')}"
    out = [f"{bar}  " + "  ".join(f for f in facts if f)]
    node = [paint(r.get("callsign") or "no spotter ID", "1" if r.get("callsign") else "31"),
            r.get("grid") or "", r.get("op_name") or "",
            "rbn upload " + paint.upload(str(r.get("rbn_upload") or "off"))]
    out.append(label(paint, "node") + "  ".join(x for x in node if x))
    if skimmer:
        out.append(label(paint, "skimmer") + skimmer)
    out.append(label(paint, "feeder") + feeder_text(r.get("feeder"), str(r.get("rbn_upload")),
                                                     paint))
    return out


def connection(station: dict[str, Any] | None) -> str:
    """The radio's connection as its station has it: Connected, Connecting, Reconnecting or Not
    connected; empty for a radio with no station. A running station reports the radio's own
    state, and is connecting until its first report."""
    if not station:
        return ""
    if station["state"] != "running":
        return "Not connected"
    return {"live": "Connected", None: "Connecting", "connecting": "Connecting",
            "halted": "Not connected", "stopped": "Not connected"}.get(
        station.get("live"), "Reconnecting")


def feeder_text(f: dict[str, Any] | None, mode: str, paint: Paint) -> str:
    """The feeder's state alone: off, signed in, or what is wrong (the counts are the web's)."""
    if mode == "off":
        return "off"
    if f is None:
        return paint("not running", "31")
    errs = int(f.get("errors") or 0)
    if errs:
        return paint(f"{errs} error{'s' if errs > 1 else ''}", "31")
    return paint("signed in", "32") if f.get("registered") else paint("not signed in", "31")


def render(snap: dict[str, Any], names: list[str] | None = None, color: bool = False) -> list[str]:
    """The machine's radios, each with its node, feeder and receivers (one row each); receivers
    with no radio file after them.  Named receivers: each one's full detail."""
    paint = Paint(color)
    now = float(snap.get("utc") or time.time())
    if names:
        return [line for n in names for line in named_lines(snap, n, now)]
    inst = list(snap.get("instances", []))
    stations = {s["radio"]: s for s in snap.get("stations", [])}
    feeders = {p["name"]: p.get("status") for p in snap.get("processes", [])
               if p.get("role") == "feeder"}
    out: list[str] = []
    shown: set[str] = set()
    known = {r["name"] for r in snap.get("radios", [])}
    bare = [f for f in snap.get("radio_files", []) if f["name"] not in known]  # no receivers yet
    for r in [*snap.get("radios", []), *bare]:
        mine = [i for i in inst if i.get("radio") == r["name"]]
        if not mine and r not in bare:
            continue
        running = sum(i["state"] == "running" for i in mine)
        procs = [i for i in mine if i.get("receiver_list")]
        lines = radio_lines(r, stations.get(r["name"]), running, paint,
                            skimmer_text(procs[0], now, paint) if len(procs) == 1 else "")
        rows = receiver_block(mine, now, paint) if mine else [
            "   no receivers yet: add them in SparkGap Web"]
        out += [*lines, "", *rows, ""]
        shown.update(i["name"] for i in mine)
    rest = [i for i in inst if i["name"] not in shown]
    if rest:
        out += [paint("━━ receivers with no radio file ━━", "1"), *receiver_block(rest, now, paint)]
        for i in rest:
            mode = str((i.get("settings") or {}).get("rbn_upload", "off"))
            out.append(f"   {i['name']}: {paint('feeder', '34')} "
                       + feeder_text(feeders.get(i["name"]), mode, paint))
    return out[:-1] if out and out[-1] == "" else out


def receiver_of(snap: dict[str, Any], name: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The process and receiver an HPSDR receiver name (<radio>-<n>) stands for, else None."""
    for i in snap.get("instances", []):
        for rr in i.get("receiver_list") or []:
            if rr["name"] == name:
                return i, rr
    return None


def process_of(snap: dict[str, Any], name: str) -> str:
    """The process a name starts or stops: an HPSDR receiver's is its radio's, as its card's Run."""
    found = receiver_of(snap, name)
    return found[0]["name"] if found else name


def web_link(root: Path) -> str:
    """The last line of status, start and stop: SparkGap Web, where everything else is."""
    try:
        cfg = json.loads((root / "server.json").read_text())
    except (OSError, ValueError):
        return ""
    web = "https" if cfg.get("tls_cert") else "http"
    return (f"Bands, receivers, decoder options and Setup: SparkGap Web, "
            f"{web}://{cfg.get('bind', '127.0.0.1')}:{cfg.get('port', 7320)}/")


def named_lines(snap: dict[str, Any], name: str, now: float) -> list[str]:
    for i in snap.get("instances", []):
        if i["name"] == name:
            return receiver_lines(i, now)
    found = receiver_of(snap, name)
    return receiver_lines(found[0], now, found[1]) if found else []


def value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def settings_of(cmd: str, args: list[str]) -> dict[str, Any]:
    """band, rec, rbn and set as the server's settings; ValueError says what is wrong."""
    if cmd == "band" and len(args) == 1:
        return {"band": args[0]}
    if cmd == "rec" and args in (["on"], ["off"]):
        return {"record": args[0] == "on"}
    if cmd == "rbn" and len(args) == 1 and args[0] in ("off", "dry", "live"):
        return {"rbn_upload": args[0]}
    if cmd == "set" and args and all("=" in a for a in args):
        return {k: value(v) for k, v in (a.split("=", 1) for a in args)}
    raise ValueError(f"usage: {cmd} " + {"band": "<name> <band>", "rec": "<name> on|off",
                                         "rbn": "<name> off|dry|live",
                                         "set": "<name> key=value ..."}[cmd])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SparkGap server command line client")
    ap.add_argument("--root", type=Path, default=default_root(), help="the run folder")
    ap.add_argument("cmd", choices=["status", "start", "stop", "restart", "band", "rec", "rbn",
                                    "set", "discover"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args(argv)
    try:
        changes = a.cmd in ("band", "rec", "rbn", "set")
        settings = settings_of(a.cmd, a.args[1:]) if changes else None
        if settings is not None and not a.args:
            raise ValueError(f"{a.cmd}: name the instance")
    except ValueError as e:
        print(f"sparkgap_ctl: {e}", file=sys.stderr)
        return EXIT_FAILED
    try:
        port, code, tls = server_settings(a.root)
        c = Client(port, code, tls=tls)
    except NoServer as e:
        print(f"sparkgap_ctl: {e}", file=sys.stderr)
        return EXIT_NO_SERVER
    try:
        link = web_link(a.root)
        if a.cmd == "status":
            color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
            print("\n".join([*render(c.snapshot(), a.args or None, color), *([link] if link else [])]))
            return 0
        if a.cmd == "discover":
            ok, text = c.request("discover", settings=discover_settings(a.args) or None)
            print(text)
            return 0 if ok else EXIT_FAILED
        snap = c.snapshot()
        if settings is not None:
            ok, text = c.request("set", a.args[0], settings)
            print(text)
            return 0 if ok else EXIT_FAILED
        names: list[str] = []
        for n in a.args or [i["name"] for i in snap.get("instances", [])]:
            n = process_of(snap, n)  # start Web888-3 starts its radio's process
            if n not in names:
                names.append(n)
        failed = False
        for name in names:
            ok, text = c.request(a.cmd, name)
            print(text if ok else f"{name}: {text}")
            failed |= not ok
        if link and a.cmd in ("start", "stop"):
            print(link)
        return EXIT_FAILED if failed else 0
    except NoServer as e:
        print(f"sparkgap_ctl: {e}", file=sys.stderr)
        return EXIT_NO_SERVER
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
