"""The SparkGap instances on one machine: start, stop, status and settings.

The run folder (docs/SERVER_SETUP.md):
<root>/instances (name, code folder, config, python), config/<name>.local.json, logs/<name>.pid,
logs/<name>_<band>_<UTC>.log, logs/station_<radio>.{pid,lock}, rec/.  Every function blocks
(a start waits for the copy to go live); the server calls them from one worker thread.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import radios

START_WAIT_S = 45  # longest a copy takes to reach the radio and go live
STOP_WAIT_S = 30
STATION_WAIT_S = 45
REC_MIN_GB = 5  # recording on refuses below this much free space in rec/
LIVE_MARK = "SparkGap LIVE:"
STATION_MARK = "holds the GUI client"
STATION_HELD = "already has station"
FEED_PORT_RE = re.compile(r"GUI feed on wss?://\S+?:(\d+)/")
ERROR_RE = re.compile(r"^[0-9:]+ ERROR |^[A-Za-z_.]*(Error|Exception): ")
SPOT_RE = re.compile(r"SPOT:")
LIVE_RE = re.compile(r"SparkGap LIVE: (\S+) \(([\d.]+) kHz\) rx(\d+)")  # one receiver's band and centre
RAW_RE = re.compile(r"ITILA raw ([\d.]+) kHz")  # one decoded window, at its frequency
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
RECEIVER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)-([1-9])$")  # <radio>-<DAX-IQ channel>
PORT_KEYS = ("flex_udp_port", "telnet_port", "telnet_local_port")
PORT_DEFAULTS = {"flex_udp_port": 7791, "telnet_port": 7300}  # sparkgap.py's, when a key is left out
# an added receiver's ports: base + DAX-IQ channel. SparkGap's block is 7320-7339 (server 7320,
# feeds 7321-7329, telnet 7330 + channel), clear of the Aggregator's 7300 and CW Skimmer's ports.
ADD_PORT_BASES = {"flex_udp_port": 7790, "telnet_port": 7330}
CODE = Path(__file__).resolve().parent  # the code this server runs: what an added receiver runs
TEMPLATES = {"flex": "skimmer_flex.example.json", "hpsdr": "skimmer_hpsdr.example.json"}  # shipped defaults
HPSDR_LISTEN_BASE = 1024  # each HPSDR radio's own local UDP port: the radio's, then the next free
# from the shipped defaults: the radio file's, or set per receiver by add
NOT_FROM_TEMPLATE = ("sdr_ip", "callsign", "grid", "op_name", "qth", "radio", "rbn_upload",
                     "flex_station", "_radio", "_comment_radio")
RADIO_RE = re.compile(
    r"^[A-Za-z0-9.:-]{1,64}$"
)  # it goes into file names: a host name or address only
VALIDATION = ("off", "permissive", "normal", "aggressive")
CALL_SOURCES = ("scp", "rbn", "rbn+skcc")
DISCOVER_S = radios.DISCOVER_S
RBN_MODES = ("off", "dry", "live")  # rbn_upload: dry logs what it would post, live posts
# decoder options by config key: on/off ones, and ones with a few values (None: off, key removed)
OPTION_FLAGS = (
    "spot_rule_split_runs",
    "spot_rule_contest_words",
    "spot_rule_cq_repeats",
    "spot_rule_doubled_calls",
    "spot_rule_split_up",
    "spot_rule_runner_signs",
    "spot_rule_window_join",
    "spot_rule_owner_cell",
    "spot_rule_owned_two_windows",
    "spot_rule_owner_listed",
    "spot_rule_la1d",
    "spot_rule_gluek",
    "spot_rule_norival",
    "itila2_snr_decoder",
)
OPTION_CHOICES: dict[str, tuple[Any, ...]] = {
    "spot_rule_fragments": (None, "any", "any,stray", "even", "even,stray"),
    "spot_rule_takeover_idle_s": (None, 120, 180, 300),
    "spot_rule_strongest_db": (None, 3, 6, 10),
    "itila_window_sec": (60, 30, 15, 7.5),
}
OPTION_DEFAULTS: dict[str, Any] = {"itila_window_sec": 60}  # where the key is read
# a Flex copy's receive path: setting -> (the copy's live key, its config key).  SparkGap follows
# the radio: a running copy changes it live; the config keys are the operator's startup values
RX_SETTINGS = {
    "antenna": ("rxant", "flex_rxant"),
    "loop_a": ("loopa", "flex_loopa"),
    "loop_b": ("loopb", "flex_loopb"),
    "rf_gain": ("rfgain", "flex_rfgain"),
}
ANTENNA_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
TOP_CALLS = 10
ERRORS_KEPT = 5
IQ_TRIMMED_RE = re.compile(r"IQ trimmed before scanner: ([\d.]+s)")


@dataclass
class LogStats:
    """Counts from one log, read on from where the last call stopped: logs reach tens of MB."""

    offset: int = 0
    partial: str = ""
    spots: Counter[str] = field(default_factory=Counter)
    spot_khz: Counter[int] = field(default_factory=Counter)  # spots by kHz: an HPSDR receiver's own
    spot_calls: set[tuple[int, str]] = field(default_factory=set)  # (kHz, call): its calls
    raw_khz: Counter[int] = field(default_factory=Counter)  # decoded windows by kHz: its windows
    live: dict[int, tuple[str, float]] = field(default_factory=dict)  # receiver index -> band, centre kHz
    errors: list[str] = field(default_factory=list)
    n_errors: int = 0
    warnings: int = 0
    reconnects: int = 0
    feed_port: int | None = None
    feed_failed: bool = False
    iq_trimmed: str | None = None

    def update(self, log: Path) -> None:
        try:
            with open(log, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return
        self.offset += len(chunk)
        *lines, self.partial = (self.partial + chunk.decode(errors="replace")).split("\n")
        for line in lines:
            if SPOT_RE.search(line) and len(line.split()) > 5:
                self.spots[line.split()[5]] += 1
                with contextlib.suppress(ValueError):
                    khz = int(float(line.split()[4]))
                    self.spot_khz[khz] += 1
                    self.spot_calls.add((khz, line.split()[5]))
            elif m := RAW_RE.search(line):
                self.raw_khz[int(float(m.group(1)))] += 1
            elif m := LIVE_RE.search(line):
                self.live[int(m.group(3))] = (m.group(1), float(m.group(2)))
            elif ERROR_RE.match(line):
                self.errors = (self.errors + [line])[-ERRORS_KEPT:]
                self.n_errors += 1
            if " WARNING " in line:
                self.warnings += 1
            if "connecting again" in line:
                self.reconnects += 1
            if m := FEED_PORT_RE.search(line):
                self.feed_port = int(m.group(1))
            elif "GUI feed failed to start" in line:
                self.feed_failed = True
            if m := IQ_TRIMMED_RE.search(line):
                self.iq_trimmed = m.group(1)


def station_view(radio: str, running: bool, health: dict[str, Any] | None) -> dict[str, Any]:
    """A radio's station as the server shows it, from its health (its link's status)."""
    out: dict[str, Any] = {"radio": radio, "state": "running" if running else "stopped"}
    if not health:
        return out
    hour = [loss for loss in health.get("losses", []) if time.time() - loss[0] < 3600]
    out.update(
        station=health.get("station"),
        live=health.get("state"),
        rtt_ms=health.get("rtt_ms"),
        losses_hour=len(hour),
        last_loss=hour[-1] if hour else None,
    )
    return out


class SeatError(RuntimeError):
    """A request the seat refuses or could not carry out; the message says why."""


def bands_in(code: Path) -> list[str]:
    """Band names a code folder's sparkgap.py accepts, from its BANDS line."""
    def parse(text: str) -> list[str]:
        m = re.search(r"^BANDS = dict\(zip\((.*)$", text, re.M)
        return re.findall(r"'(\d+m)'", m.group(1)) if m else []

    try:
        return list(cached(code / "sparkgap.py", parse))
    except OSError:
        return []


def decoder_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """A config's decoder, spot rule and recording settings, as SparkGap Web shows them."""
    return {
        "validation": cfg.get("spot_validation") or "off",
        "call_source": cfg.get("spot_validation_calls") or "scp",
        "afc": bool(cfg.get("itila2_afc", False)),
        "decoder": cfg.get("cw_decoder", "itila2"),  # shown only: AFC acts with itila2
        **{k: bool(cfg.get(k, False)) for k in OPTION_FLAGS},
        **{k: cfg.get(k, OPTION_DEFAULTS.get(k)) for k in OPTION_CHOICES},
        "record": bool(cfg.get("record_wav")),
    }


@dataclass(frozen=True)
class Instance:
    name: str
    code: Path
    config: Path
    python: str


def utc_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%SZ", time.gmtime())


def errors_in(log: Path) -> list[str]:
    """A log's ERROR lines and the exception that ends a traceback."""
    try:
        return [
            line for line in log.read_text(errors="replace").splitlines() if ERROR_RE.match(line)
        ]
    except OSError:
        return []


def proc_started(pid: int) -> float | None:
    """When process pid started, in seconds since the epoch, from /proc (as ps reckons its age)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        ticks = int(
            stat.rsplit(")", 1)[1].split()[19]
        )  # field 22, starttime, after the command name
        boot = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return boot + ticks / os.sysconf("SC_CLK_TCK")


_CACHE: dict[Path, tuple[tuple[int, int, int], Any]] = {}
_CACHE_LOCK = threading.Lock()


def cached(path: Path, parse: Callable[[str], Any]) -> Any:
    """parse(path's text), read again only when the file changes (inode, mtime or size: a
    config write replaces the file): the server asks for each config and band list every 2 s."""
    st = path.stat()
    key = (st.st_ino, st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        hit = _CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    value = parse(path.read_text(errors="replace"))
    with _CACHE_LOCK:
        _CACHE[path] = (key, value)
    return value


def _port_free(port: int, udp: bool = False, host: str | None = None) -> bool:
    """Whether this machine has port free (TCP on the telnet address, or UDP), as sparkgap.py binds it."""
    with socket.socket(type=socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as s:
        if not udp:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # asyncio's servers set it too
        try:
            s.bind(("" if udp else host or "0.0.0.0", port))
        except OSError:
            return False
    return True


def _ports(c: dict[str, Any]) -> list[int]:
    """The ports a receiver's config takes: UDP, telnet and its feed when fixed."""
    feed = c.get("gui_feed", {}).get("port") if isinstance(c.get("gui_feed"), dict) else None
    ports = [p for k in PORT_KEYS if isinstance(p := c.get(k, PORT_DEFAULTS.get(k)), int)]
    if c.get("sdr_type") != "flex" and "flex_udp_port" not in c:
        ports.remove(PORT_DEFAULTS["flex_udp_port"])  # only a Flex copy takes it
    return ports + ([feed] if isinstance(feed, int) else [])


def write_config(path: Path, changes: dict[str, Any]) -> None:
    """Set keys in a JSON config (None removes one), one atomic replace. Each top-level key on a
    line of its own is edited in place, so the file's own layout, key order and comments-free hand
    formatting stay; anything else is written whole with its indentation."""
    text = path.read_text()
    cfg = json.loads(text)
    want = dict(cfg)
    for key, value in changes.items():
        if value is None:
            want.pop(key, None)
        else:
            want[key] = value
    out = _edit_lines(text, cfg, changes)
    if out is None or json.loads(out) != want:
        m = re.search(r'\n([ \t]+)"', text)
        out = json.dumps(want, indent=m.group(1) if m else 2) + "\n"
    new = path.with_name(path.name + ".new")
    new.write_text(out)
    os.replace(new, path)


def _edit_lines(text: str, cfg: dict[str, Any], changes: dict[str, Any]) -> str | None:
    """text with each change made on its own line, or None when a key's line can't be found."""
    m = re.search(r'\n([ \t]+)"', text)
    if not m:
        return None
    indent, lines = m.group(1), text.split("\n")
    for key, value in changes.items():
        head = f'{indent}{json.dumps(key)}'
        at = [
            n
            for n, ln in enumerate(lines)
            if ln.startswith(head) and ln[len(head) :].lstrip().startswith(":")
        ]
        if len(at) > 1:
            return None
        if at:
            n = at[0]
            body = lines[n][len(head):].lstrip()[1:].strip()
            comma = body.endswith(",")
            try:
                if json.loads(body.rstrip(",")) != cfg.get(key):
                    return None  # the value runs over several lines
            except ValueError:
                return None
            if value is not None:
                lines[n] = f"{head}: {json.dumps(value)}{',' if comma else ''}"
                continue
            del lines[n]
            if not comma:  # it was the last key: the one before it loses its comma
                k = n - 1
                while k >= 0 and not lines[k].strip():
                    k -= 1
                if k < 0 or not lines[k].rstrip().endswith(","):
                    return None
                lines[k] = lines[k].rstrip()[:-1]
        elif value is not None:
            k = len(lines) - 1
            while k >= 0 and lines[k].strip() != "}":
                k -= 1
            j = k - 1
            while j >= 0 and not lines[j].strip():
                j -= 1
            if k < 0 or j < 0:
                return None
            if lines[j].strip() != "{" and not lines[j].rstrip().endswith(","):
                lines[j] = lines[j].rstrip() + ","
            lines.insert(k, f"{head}: {json.dumps(value)}")
    return "\n".join(lines)


class Seat:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.logs = root / "logs"
        self.rec = root / "rec"
        self._stats: dict[Path, LogStats] = {}
        self._stats_lock = threading.Lock()  # status() runs on the server's threads at once

    # ---- instances and their configs ----

    def instances(self) -> list[Instance]:
        out: list[Instance] = []
        path = self.root / "instances"
        for line in path.read_text().splitlines() if path.exists() else []:
            parts = line.split()
            if len(parts) < 4 or line.lstrip().startswith("#") or not NAME_RE.match(parts[0]):
                continue
            out.append(
                Instance(parts[0], Path(parts[1]), self.root / "config" / parts[2], parts[3])
            )
        return out

    def instance(self, name: str) -> Instance:
        for i in self.instances():
            if i.name == name:
                return i
        raise SeatError(f"unknown instance {name!r}")

    @staticmethod
    def config(i: Instance) -> dict[str, Any]:
        def parse(text: str) -> dict[str, Any]:
            cfg: Any = json.loads(text)
            return cfg if isinstance(cfg, dict) else {}

        return dict(cached(i.config, parse))

    def band(self, i: Instance) -> str:
        bands = self.config(i).get("bands") or ["?"]
        return "+".join(str(b) for b in bands)

    def multi_band(self, i: Instance) -> bool:
        """Several bands in one copy: its bands are set in its config, never from the web."""
        return len(self.config(i).get("bands") or []) > 1

    def radio(self, i: Instance) -> str | None:
        """The Flex radio an instance uses, by the name its station goes by: radio (the radio's
        nickname, its radio file from Discover), else its sdr_ip; None for any other receiver."""
        cfg = self.config(i)
        if not radios.capabilities(cfg.get("sdr_type")).station:
            return None
        if not (cfg.get("radio") or cfg.get("sdr_ip")):
            return None
        radio = str(cfg.get("radio") or cfg["sdr_ip"])
        if not RADIO_RE.match(radio):
            raise SeatError(f"{i.name}: sdr_ip {radio!r} is not a host name or address")
        return radio

    @staticmethod
    def bands_known(i: Instance) -> list[str]:
        """Band names the instance's sparkgap.py accepts, from its BANDS line."""
        return bands_in(i.code)

    # ---- process files ----

    def pid(self, name: str) -> int | None:
        return self._alive(self.logs / f"{name}.pid", "sparkgap.py")

    def feeder_pid(self, name: str) -> int | None:
        return self._alive(self.logs / f"rbn2_{name}.pid", "rbn2_feeder.py")

    def station_pid(self, radio: str) -> int | None:
        return self._alive(self.logs / f"station_{radio}.pid", "flex_station.py")

    @staticmethod
    def _alive(pidfile: Path, program: str) -> int | None:
        """The pid in pidfile while that process is still the program it names: after a crash the
        number can be reused, and a stop must never signal some other process."""
        try:
            pid = int(pidfile.read_text().strip())
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except (OSError, ValueError):
            return None
        return pid if any(Path(a.decode(errors="replace")).name == program for a in argv) else None

    def newest_log(self, prefix: str) -> Path | None:
        logs = sorted(self.logs.glob(f"{prefix}_*.log"), key=lambda p: p.stat().st_mtime)
        return logs[-1] if logs else None

    @contextlib.contextmanager
    def lock(self, i: Instance) -> Iterator[None]:
        """One start, stop or config write at a time: the radio's lock, then the instance's."""
        self.logs.mkdir(parents=True, exist_ok=True)
        radio = self.radio(i)
        with contextlib.ExitStack() as held:
            for name in ([f"station_{radio}.lock"] if radio else []) + [f"{i.name}.lock"]:
                f = held.enter_context(open(self.logs / name, "w"))
                fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def _launch(self, cwd: Path, pidfile: Path, log: Path, argv: list[str]) -> None:
        """Start argv in its own session, detached from us (a setsid subshell):
        it keeps running when the server stops, and init reaps it."""
        inner = 'echo $$ > "$2"; log=$3; shift 3; exec "$@" > "$log" 2>&1'
        script = f'cd "$1" && (setsid bash -c \'{inner}\' _ "$@" < /dev/null > /dev/null 2>&1 &)'
        subprocess.run(
            ["bash", "-c", script, "_", str(cwd), str(pidfile), str(log), *argv],
            check=True,
            close_fds=True,
            stdin=subprocess.DEVNULL,
        )

    @staticmethod
    def _wait_for(log: Path, mark: str, alive: Any, wait_s: float) -> bool:
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            time.sleep(1)
            try:
                if mark in log.read_text(errors="replace"):
                    return True
            except OSError:
                pass
            if alive() is None:
                return False
        return False

    # ---- the radio's station ----

    def station_up(self, i: Instance) -> str | None:
        radio = self.radio(i)
        if radio is None or self.station_pid(radio):
            return None
        log = self.logs / f"station_{radio}_{utc_stamp()}.log"
        pidfile = self.logs / f"station_{radio}.pid"
        self._launch(
            i.code,
            pidfile,
            log,
            [
                i.python,
                "flex_station.py",
                "--config",
                str(i.config),
            ],
        )
        if self._wait_for(log, STATION_MARK, lambda: self.station_pid(radio), STATION_WAIT_S):
            return f"station {radio}: live"
        pidfile.unlink(missing_ok=True)
        try:
            held = STATION_HELD in log.read_text(errors="replace")
        except OSError:
            held = False
        if held:
            return f"station {radio}: held by another process"
        last = (errors_in(log) or ["no error logged"])[-1]
        raise SeatError(f"station {radio} failed to start: {last}")

    def station_down(self, i: Instance) -> str | None:
        radio = self.radio(i)
        if radio is None:
            return None
        for j in self.instances():
            if j.name != i.name and self.pid(j.name) and self.radio(j) == radio:
                return None
        pid = self.station_pid(radio)
        pidfile = self.logs / f"station_{radio}.pid"
        if pid is None:
            pidfile.unlink(missing_ok=True)
            return None
        if not self._signal_and_wait(pid, STOP_WAIT_S):
            raise SeatError(f"station {radio} still running after {STOP_WAIT_S} s (pid {pid})")
        pidfile.unlink(missing_ok=True)
        return f"station {radio}: stopped"

    @staticmethod
    def _signal_and_wait(pid: int, wait_s: float) -> bool:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGINT)
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(1)
        return False

    # ---- copies ----

    def start(self, name: str) -> str:
        i = self.instance(name)
        with self.lock(i):
            return self._start(i)

    def _start(self, i: Instance) -> str:
        """Start a copy; the caller holds its lock."""
        name = i.name
        if pid := self.pid(name):
            return f"{name}: already running (pid {pid})"
        self.station_up(i)
        log = self.logs / f"{name}_{self.band(i)}_{utc_stamp()}.log"
        pidfile = self.logs / f"{name}.pid"
        self._launch(i.code, pidfile, log, [i.python, "sparkgap.py", "--config", str(i.config)])
        if self._wait_for(log, LIVE_MARK, lambda: self.pid(name), START_WAIT_S):
            return f"{name}: live (pid {self.pid(name)})"
        if self.pid(name):
            raise SeatError(f"{name}: running but not live after {START_WAIT_S} s, see {log.name}")
        pidfile.unlink(missing_ok=True)
        raise SeatError(f"{name}: failed to start: {(errors_in(log) or ['no error logged'])[-1]}")

    def stop(self, name: str) -> str:
        i = self.instance(name)
        with self.lock(i):
            return self._stop(i)

    def _stop(self, i: Instance) -> str:
        """Stop a copy, and its station after the radio's last copy; the caller holds its lock."""
        name = i.name
        pid = self.pid(name)
        if pid and not self._signal_and_wait(pid, STOP_WAIT_S):
            raise SeatError(f"{name}: still running after {STOP_WAIT_S} s (pid {pid})")
        (self.logs / f"{name}.pid").unlink(missing_ok=True)
        self.station_down(i)
        return f"{name}: stopped" if pid else f"{name}: not running"

    def restart(self, name: str) -> str:
        i = self.instance(name)
        with self.lock(i):
            self._stop(i)
            return self._start(i)

    # ---- settings: a fixed list, each applied by a restart ----

    def settings(self, i: Instance) -> dict[str, Any]:
        cfg = self.config(i)
        return {
            "band": self.band(i),
            "bands_known": self.bands_known(i),
            "multi_band": self.multi_band(i),
            **decoder_settings(cfg),
            "rbn_upload": self.upload_mode(i),
            # a Flex copy's receive path as its config sets it at start (None: the radio's own)
            **{k: cfg.get(c) for k, (_, c) in RX_SETTINGS.items()},
        }

    def removed_settings(self, listed: set[str]) -> dict[str, dict[str, Any]]:
        """Receivers removed but whose config stays, by name: what adding the name again brings
        back."""
        out: dict[str, dict[str, Any]] = {}
        for path in sorted((self.root / "config").glob("*.local.json")):
            name = path.name.removesuffix(".local.json")
            if name in listed or not RECEIVER_RE.match(name):
                continue
            try:
                cfg = cached(path, json.loads)
            except (OSError, ValueError):
                continue
            if isinstance(cfg, dict) and cfg.get("radio"):
                out[name] = decoder_settings(cfg)
        return out

    def default_settings(self) -> dict[str, Any]:
        """The shipped defaults an added receiver starts with and Reset goes back to."""
        try:
            return decoder_settings(self._template())
        except SeatError:
            return {}

    def change(
        self,
        name: str,
        settings: dict[str, Any],
        live: Callable[[str, str], str | None] | None = None,
        live_rx: Callable[[str, dict[str, Any]], tuple[bool, str] | None] | None = None,
    ) -> str:
        """Check every setting, write them all at once, restart the copy if it runs. A band change
        alone first goes to live(name, band), which moves the running copy in place and returns its
        answer, None when it could not (then the copy restarts, onto the band just written), or
        raises SeatError while the copy recovers from a radio outage (nothing written).
        Receive path settings (RX_SETTINGS) on a running copy go to live_rx(name, change) only;
        given to a stopped copy, they are written as its startup values."""
        i = self.instance(name)
        changes: dict[str, Any] = {}
        rx: dict[str, Any] = {}
        for key, value in settings.items():
            if key in RX_SETTINGS:
                self._check_rx(i, key, value)
                rx[key] = value
            elif key == "band":
                if self.multi_band(i):
                    raise SeatError(f"{name} runs {self.band(i)}: set its bands in its config")
                if value not in self.bands_known(i):
                    raise SeatError(f"band must be one of: {' '.join(self.bands_known(i))}")
                changes["bands"] = [value]
            elif key == "rx_bands":  # {receiver number: band}, an HPSDR radio's receivers
                changes["bands"] = self._rx_bands(i, value)
            elif key == "validation":
                if value not in VALIDATION:
                    raise SeatError(f"validation must be one of: {', '.join(VALIDATION)}")
                changes["spot_validation"] = value
            elif key == "call_source":
                if value not in CALL_SOURCES:
                    raise SeatError(f"call source must be one of: {', '.join(CALL_SOURCES)}")
                changes["spot_validation_calls"] = value
            elif key in OPTION_FLAGS:
                if not isinstance(value, bool):
                    raise SeatError(f"{key} must be true or false")
                changes[key] = value
            elif key in OPTION_CHOICES:
                if isinstance(value, bool) or value not in OPTION_CHOICES[key]:
                    shown = ", ".join("off" if v is None else str(v) for v in OPTION_CHOICES[key])
                    raise SeatError(f"{key} must be one of: {shown}")
                changes[key] = value
            elif key == "afc":
                if not isinstance(value, bool):
                    raise SeatError("afc must be true or false")
                changes["itila2_afc"] = value
            elif key == "record":
                if not isinstance(value, bool):
                    raise SeatError("record must be true or false")
                if value:
                    self.rec.mkdir(parents=True, exist_ok=True)
                    free_gb = shutil.disk_usage(self.rec).free // 2**30
                    if free_gb < REC_MIN_GB:
                        raise SeatError(
                            f"only {free_gb} GB free in rec/ (recording needs {REC_MIN_GB})"
                        )
                changes["record_wav"] = (
                    str(self.rec / f"{name}_{{band}}_{{ts}}.wav") if value else None
                )
            else:
                raise SeatError(f"{key} cannot be changed here")
        if not changes and not rx:
            raise SeatError("nothing to change")
        done = f"{name}: {', '.join(f'{k} {v}' for k, v in settings.items())}"
        rest = {k: v for k, v in settings.items() if k not in rx}
        with self.lock(i):  # the write and its restart as one step: nothing slips in between
            running = bool(self.pid(name))
            if rx and running:
                change = {RX_SETTINGS[k][0]: v for k, v in rx.items()}
                took = live_rx(name, change) if live_rx else None
                if took is None:
                    raise SeatError(f"{name} did not answer; nothing changed")
                if not took[0]:
                    raise SeatError(f"{name}: {took[1]}")
                done = f"{name}: {took[1]}"
            elif rx:
                changes.update({RX_SETTINGS[k][1]: v for k, v in rx.items()})
            answer = None
            if running and live is not None and set(rest) == {"band"}:
                answer = live(name, settings["band"])  # before the write: a refusal changes nothing
            if changes:
                write_config(i.config, changes)
            if not running or not rest:
                return done
            if answer:
                return f"{name}: {answer}"
            self._stop(i)
            return f"{done}; {self._start(i)}"

    def _rx_bands(self, i: Instance, value: Any) -> list[str]:
        """The bands list with some of an HPSDR radio's receivers moved; SeatError: refused."""
        cfg = self.config(i)
        bands = list(cfg.get("bands") or [])
        if cfg.get("sdr_type") != "hpsdr" or not all(isinstance(b, str) for b in bands):
            raise SeatError(f"{i.name}: only an HPSDR radio's receivers, with bands by name, move here")
        if not isinstance(value, dict) or not value:
            raise SeatError("rx_bands must be {receiver number: band}")
        known = self.bands_known(i)
        for n, band in value.items():
            k = int(n) if str(n).isdigit() else 0
            if not 1 <= k <= len(bands):
                raise SeatError(f"{i.name} has receivers 1 to {len(bands)}")
            if band not in known:
                raise SeatError(f"band must be one of: {' '.join(known)}")
            bands[k - 1] = band
        if len(set(bands)) != len(bands):
            raise SeatError("two receivers on one band")
        return bands

    def _check_rx(self, i: Instance, key: str, value: Any) -> None:
        sdr = self.config(i).get("sdr_type")
        if key not in radios.capabilities(sdr).rx_settings or self.radio(i) is None:
            raise SeatError(f"{key}: a {sdr or 'this'} receiver has no {key} setting")
        if key == "antenna" and not (isinstance(value, str) and ANTENNA_RE.match(value)):
            raise SeatError("antenna must be one the radio lists")
        if key in ("loop_a", "loop_b") and not isinstance(value, bool):
            raise SeatError(f"{key} must be true or false")
        if key == "rf_gain" and (isinstance(value, bool) or not isinstance(value, int)):
            raise SeatError("rf_gain must be a whole number of dB")

    # ---- receivers added and removed (SparkGap Web) ----

    @contextlib.contextmanager
    def _instances_lock(self) -> Iterator[None]:
        self.logs.mkdir(parents=True, exist_ok=True)
        with open(self.logs / "instances.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def add(self, name: str, band: str) -> str:
        """A new Flex receiver <radio>-<n> on DAX-IQ channel n: its config from the shipped defaults (or
        the one it had before it was removed) and its instances line, running the server's code.
        Antenna and RF gain are left out (it starts with the radio's); spotter ID and grid are
        its radio file's."""
        m = RECEIVER_RE.match(name)
        if not m:
            raise SeatError("name must be <radio>-<n>: the radio's name and its DAX-IQ channel")
        rn, num = m.group(1), int(m.group(2))
        bands = bands_in(CODE)
        if band not in bands:
            raise SeatError(f"band must be one of: {' '.join(bands)}")
        try:
            radio = json.loads((self.root / "config" / f"{rn}.radio.json").read_text())
        except (OSError, ValueError):
            raise SeatError(f"no radio file for {rn}: Discover it in Setup first") from None
        if missing := [k for k in ("callsign", "grid") if not radio.get(k)]:
            raise SeatError(f"{rn} has no {' or '.join(missing)}: set them in Setup first")
        if radio.get("type") == "hpsdr":
            return self._add_hpsdr(rn, num, radio, band)
        if radio.get("type") != "flex":
            raise SeatError(f"{rn} is a {radio.get('type')} radio: its receivers are not added here")
        chans = radio.get("daxiq_channels")
        if isinstance(chans, int) and num > chans:
            raise SeatError(f"the {radio.get('model') or rn} has DAX-IQ channels 1 to {chans}")
        conf = self.root / "config" / f"{name}.local.json"
        with self._instances_lock():
            existing = self.instances()
            if any(i.name == name for i in existing):
                raise SeatError(f"{name} already exists")
            cfgs = {i.name: self.config(i) for i in existing}
            mine = [n for n, c in cfgs.items() if c.get("radio") == rn or (  # or set up by address
                c.get("sdr_type") == "flex" and not c.get("radio") and c.get("sdr_ip") == radio.get("address"))]
            caps = [radio.get(k) for k in ("max_panadapters", "max_slices", "daxiq_channels")]
            limit = min([v for v in caps if isinstance(v, int) and v > 0], default=None)
            if limit is not None and len(mine) >= limit:
                raise SeatError(f"the {radio.get('model') or rn} holds {limit} receivers: all in use")
            for n in mine:
                if cfgs[n].get("flex_daxiq_channel") == num:
                    raise SeatError(f"DAX-IQ {num} is already used by {n}")
            used = {p for c in cfgs.values() for p in _ports(c)}
            reused = conf.exists()
            if reused:
                c = json.loads(conf.read_text())
                if c.get("radio") not in (None, rn) or c.get("flex_daxiq_channel") not in (None, num):
                    raise SeatError(f"{conf.name} is for {c.get('radio')} DAX-IQ "
                                    f"{c.get('flex_daxiq_channel')}, not {rn} {num}")
            else:
                c = self._template()
                if isinstance(c.get("gui_feed"), dict):
                    c["gui_feed"]["port"] = "auto"
                c["rbn_known_dir"] = str(self.root / "rbn_known")
                if c.get("record_wav"):
                    c["record_wav"] = str(self.rec / f"{name}_{{band}}_{{ts}}.wav")
            for k, base in ADD_PORT_BASES.items():  # base + channel, or the next one free on this machine
                c[k] = base + num
                while c[k] in used or not _port_free(c[k], udp=k == "flex_udp_port", host=c.get("telnet_host")):
                    c[k] += 1
                used.add(c[k])
            c.update(radio=rn, flex_daxiq_channel=num, bands=[band])
            for k in ("callsign", "grid", "skimmer_suffix"):  # the radio file's spotter ID and grid
                c.pop(k, None)
            c.pop("sdr_ip", None)  # the radio file's address
            new = conf.with_name(conf.name + ".new")
            new.write_text(json.dumps(c, indent=2) + "\n")
            os.replace(new, conf)
            with open(self.root / "instances", "a") as f:
                f.write(f"{name} {CODE} {conf.name} {sys.executable}\n")
        how = "its earlier config" if reused else "the shipped defaults"
        return f"{name}: added on DAX-IQ {num}, {band}, from {how}"

    def _add_hpsdr(self, rn: str, num: int, radio: dict[str, Any], band: str) -> str:
        """An HPSDR radio's next receiver: its one process gets the band (made from the shipped
        HPSDR defaults with its first receiver, with its own telnet and listen ports) and
        restarts if it runs. Receivers are numbered by their place in bands[]."""
        conf = self.root / "config" / f"{rn}.local.json"
        with self._instances_lock():
            existing = self.instances()
            i = next((x for x in existing if x.name == rn), None)
            if i is not None and self.config(i).get("sdr_type") != "hpsdr":
                raise SeatError(f"{rn} is already a receiver's name")
            if i is None and (who := self.name_taken(rn)):
                raise SeatError(f"{rn}'s receivers would share names with {who}: rename the radio in Setup")
            bands = list(self.config(i).get("bands") or []) if i else []
            limit = radio.get("receivers")
            if num != len(bands) + 1:
                raise SeatError(f"the next receiver of {rn} is {rn}-{len(bands) + 1}")
            if isinstance(limit, int) and num > limit:
                raise SeatError(f"the {radio.get('model') or rn} has receivers 1 to {limit}")
            if band in bands:
                raise SeatError(f"{rn} already has a receiver on {band}")
            if i is None:
                cfgs = [self.config(x) for x in existing]
                reused = conf.exists()
                c = json.loads(conf.read_text()) if reused else self._template("hpsdr")
                if reused and c.get("radio") not in (None, rn):
                    raise SeatError(f"{conf.name} is {c.get('radio')}'s, not {rn}'s")
                if not reused:
                    if isinstance(c.get("gui_feed"), dict):
                        c["gui_feed"]["port"] = "auto"
                    c["rbn_known_dir"] = str(self.root / "rbn_known")
                    if (radio.get("board_id"), radio.get("firmware")) == radios.WEB888:
                        c["ppm_offset"] = 3.9  # cancels the Red Pitaya correction in hpsdr_fast.c
                    if isinstance(limit, int):
                        c["max_receivers"] = limit
                used = {p for x in cfgs for p in _ports(x)}
                c["telnet_port"] = ADD_PORT_BASES["telnet_port"]
                while c["telnet_port"] in used or not _port_free(c["telnet_port"], host=c.get("telnet_host")):
                    c["telnet_port"] += 1
                listens = {x.get("hpsdr_listen_port", x.get("sdr_port", HPSDR_LISTEN_BASE))
                           for x in cfgs if x.get("sdr_type") == "hpsdr"}
                c["hpsdr_listen_port"] = HPSDR_LISTEN_BASE
                while c["hpsdr_listen_port"] in listens or not _port_free(c["hpsdr_listen_port"], udp=True):
                    c["hpsdr_listen_port"] += 1
                c.update(radio=rn, sdr_type="hpsdr", bands=[band])
                for k in ("callsign", "grid", "skimmer_suffix", "sdr_ip"):  # its radio file's
                    c.pop(k, None)
                new = conf.with_name(conf.name + ".new")
                new.write_text(json.dumps(c, indent=2) + "\n")
                os.replace(new, conf)
                with open(self.root / "instances", "a") as f:
                    f.write(f"{rn} {CODE} {conf.name} {sys.executable}\n")
                how = "its earlier config" if reused else "the shipped HPSDR defaults"
                return f"{rn}-{num}: added, {band}; {rn} runs it, from {how}"
        with self.lock(i):  # the radio's process: the band, then a restart if it runs
            write_config(i.config, {"bands": [*bands, band]})
            if not self.pid(rn):
                return f"{rn}-{num}: added, {band}; used at {rn}'s next start"
            self._stop(i)
            return f"{rn}-{num}: added, {band}; {self._start(i)}"

    def name_taken(self, name: str, own: str | None = None) -> str | None:
        """Who already uses a radio name: a receiver or process named <name> or <name>-<n>, other
        than the HPSDR process of the radio own; None when it is free."""
        for i in self.instances():
            c = self.config(i)
            mine = own is not None and c.get("radio") == own and c.get("sdr_type") == "hpsdr"
            if (i.name == name or re.match(rf"^{re.escape(name)}-\d+$", i.name)) and not mine:
                return i.name
        return None

    def withdraw(self, name: str) -> None:
        """Take a stopped copy's instances line out, its config kept (an add that did not finish)."""
        with self._instances_lock():
            if self.pid(name):
                return
            path = self.root / "instances"
            lines = [x for x in path.read_text().splitlines() if x.split()[:1] != [name]]
            tmp = path.with_name("instances.new")
            tmp.write_text("\n".join(lines) + "\n")
            os.replace(tmp, path)

    def process_name(self, name: str) -> str:
        """The instance a name stands for: an HPSDR receiver's (<radio>-<n>) is its radio's."""
        if any(i.name == name for i in self.instances()):
            return name
        m = RECEIVER_RE.match(name)
        if m and any(i.name == m.group(1) and self.config(i).get("sdr_type") == "hpsdr"
                     for i in self.instances()):
            return m.group(1)
        return name

    def rename_radio(self, old: str, new: str) -> str:
        """An HPSDR radio's new name, while it is stopped: its radio file, its receivers' radio,
        and its process's instances line and config. Logs keep their names."""
        config = self.root / "config"
        if not radios.NAME_RE.match(old):
            raise SeatError(f"{old!r}: not a radio name")
        path, to = config / f"{old}.radio.json", config / f"{new}.radio.json"
        if why := radios.check_radio_name(new):
            raise SeatError(f"name: {why}")
        if who := self.name_taken(new):
            raise SeatError(f"{new} is already used by {who}")
        try:
            st: Any = json.loads(path.read_text())
        except (OSError, ValueError):
            raise SeatError(f"no radio file for {old}") from None
        if not isinstance(st, dict) or st.get("type") != "hpsdr":
            raise SeatError(f"{old}: a FlexRadio takes its name from the radio (SmartSDR)")
        if to.exists():
            raise SeatError(f"{new} is another radio's name")
        with self._instances_lock():
            mine = [i for i in self.instances() if self.config(i).get("radio") == old]
            if running := [i.name for i in mine if self.pid(i.name)]:
                raise SeatError(f"stop {', '.join(running)} first")
            if self.feeder_pid(old):
                raise SeatError(f"{old}'s feeder runs: stop it first")
            if (config / f"{new}.local.json").exists():
                raise SeatError(f"config/{new}.local.json exists: another radio's")
            # the process's instances line and config move first, the radio file last: a failure
            # part way leaves a copy that still loads (its radio key then names the old file)
            out, moved = [], None
            for line in (self.root / "instances").read_text().splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[0] == old:
                    moved = config / parts[2]
                    line = line.replace(old, new, 1).replace(parts[2], f"{new}.local.json", 1)
                out.append(line)
            if moved is not None:
                os.replace(moved, config / f"{new}.local.json")
            tmp = self.root / "instances.new"
            tmp.write_text("\n".join(out) + "\n")
            os.replace(tmp, self.root / "instances")
            for i in self.instances():
                if self.config(i).get("radio") == old:
                    write_config(i.config, {"radio": new})
            radios.write_radio(to, {**st, "nickname": new})
            path.unlink()
        return f"{old} renamed {new}: its radio file, {len(mine)} config(s) and its receivers"

    def _template(self, kind: str = "flex") -> dict[str, Any]:
        """The shipped defaults (the code's example for the radio type) without what the radio
        file holds: an update moves the baseline for every install."""
        path = CODE / TEMPLATES[kind]
        try:
            c = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            raise SeatError(f"no shipped defaults: {e}") from None
        for k in NOT_FROM_TEMPLATE:
            c.pop(k, None)
        if not (c.get("flex_loopa") or c.get("flex_loopb")):  # a loop keeps its antenna (README)
            for k in ("flex_rxant", "flex_rfgain"):  # it starts with the radio's
                c.pop(k, None)
        c["sdr_type"] = kind
        return c

    def remove(self, name: str) -> str:
        """Take a stopped receiver out of instances; its config, logs and recordings stay, so
        adding the same name again brings its settings back.  An HPSDR receiver leaves its radio's
        bands; the last one takes the radio's process out."""
        if (proc := self.process_name(name)) != name:
            return self._remove_hpsdr(name, proc)
        i = self.instance(name)
        if self.config(i).get("sdr_type") != "flex" or not self.radio_file(i):
            raise SeatError(f"{name}: only a Flex receiver of a radio file is removed here")
        path = self.root / "instances"
        with self.lock(i), self._instances_lock():  # start holds lock(i): not removed mid-start
            if self.pid(name):
                raise SeatError(f"{name} is running; stop it first")
            lines = path.read_text().splitlines()
            keep = [x for x in lines if x.split()[:1] != [name] or x.lstrip().startswith("#")]
            new = path.with_name(path.name + ".new")
            new.write_text("".join(x + "\n" for x in keep))
            os.replace(new, path)
        return f"{name}: removed; its config stays in config/{i.config.name}"

    def receiver_count(self, proc: str) -> int:
        """How many receivers an HPSDR radio's process holds (its bands)."""
        return len(self.config(self.instance(proc)).get("bands") or [])

    def _remove_hpsdr(self, name: str, proc: str) -> str:
        """One receiver out of its HPSDR radio's bands; the ones after it move up a number (they
        are numbered by place). The radio restarts if it runs; the last receiver, stopped, takes
        the process's instances line out, its config kept."""
        i = self.instance(proc)
        num = int(RECEIVER_RE.match(name).group(2))  # process_name matched it
        with self.lock(i):
            bands = list(self.config(i).get("bands") or [])
            if not 1 <= num <= len(bands):
                raise SeatError(f"{proc} has receivers 1 to {len(bands)}")
            if len(bands) == 1:
                if self.pid(proc):
                    raise SeatError(f"{proc} is running; stop it first")
                self.withdraw(proc)
                return f"{name}: removed, {proc}'s last receiver; its config stays in config/{i.config.name}"
            write_config(i.config, {"bands": bands[:num - 1] + bands[num:]})
            moved = (f"; {proc}-{num + 1} to -{len(bands)} are now {proc}-{num} to -{len(bands) - 1}"
                     if num < len(bands) else "")
            done = f"{name}: removed ({bands[num - 1]}){moved}"
            if not self.pid(proc):
                return done
            self._stop(i)
            return f"{done}; {self._start(i)}"

    def remove_radio(self, radio: str) -> str:
        """A radio with no receivers left, out of Setup and the radio switch: its file renamed
        <name>.radio.json.removed-<time>, kept for its node settings."""
        if not radios.NAME_RE.match(radio):
            raise SeatError(f"{radio!r}: not a radio name")
        path = self.root / "config" / f"{radio}.radio.json"
        if not path.exists():
            raise SeatError(f"no radio file for {radio}")
        with self._instances_lock():
            if users := [i.name for i in self.instances() if self.config(i).get("radio") == radio]:
                raise SeatError(f"{radio} still has {', '.join(users)}: remove them first")
            if self.feeder_pid(radio) or self.station_pid(radio):
                raise SeatError(f"{radio}'s feeder or station runs: stop it first")
            to = path.with_name(f"{path.name}.removed-{utc_stamp()}")
            os.replace(path, to)
        return f"{radio}: removed; its radio file stays as config/{to.name}"

    # ---- the RBN feeder: the server starts it with its copy and stops it through its link ----

    def radio_file(self, i: Instance) -> Path | None:
        """The radio file of an instance that names its radio, else None."""
        name = self.config(i).get("radio")
        if not isinstance(name, str) or not name:
            return None
        return self.root / "config" / f"{name}.radio.json"

    def node(self, i: Instance) -> dict[str, Any]:
        """The node settings an instance feeds RBN under: its radio file, else its own config."""
        path = self.radio_file(i)
        if path is None:
            return self.config(i)
        try:
            st: Any = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        return st if isinstance(st, dict) else {}

    def feeder_name(self, i: Instance) -> str:
        """The RBN feeder an instance feeds: its radio's (one per radio), else its own."""
        return str(self.config(i)["radio"]) if self.radio_file(i) else i.name

    def upload_mode(self, i: Instance) -> str:
        v = self.node(i).get("rbn_upload")
        return v if v in ("dry", "live") else "off"

    def set_upload(self, i: Instance, mode: str | None) -> None:
        """rbn_upload in the radio file for a radio's node, else in the instance's config."""
        path = self.radio_file(i)
        if path is None:
            write_config(i.config, {"rbn_upload": mode})
            return
        try:
            st = {k: v for k, v in json.loads(path.read_text()).items() if k != "rbn_upload"}
        except (OSError, ValueError, AttributeError) as e:
            raise SeatError(f"{path.name}: {e}: run Discover") from None
        radios.write_radio(path, {**st, **({"rbn_upload": mode} if mode else {})})

    def spawn_feeder(self, i: Instance, receivers: list[str] | None = None) -> Path:
        """Start the RBN feeder (rbn2_feeder.py) for i: its radio's node, fed from receivers, or
        i's own; it links to the server's hub, which is how the server knows it runs. Its log."""
        name = self.feeder_name(i)
        path = self.radio_file(i)
        src = ["--config", str(i.config)] if path is None else ["--radio", str(path)]
        rx = [a for r in receivers or [i.name] for a in ("--receiver", r)] if path else []
        log = self.logs / f"rbn2_{name}_{utc_stamp()}.log"
        self._launch(i.code, self.logs / f"rbn2_{name}.pid", log,
                     [i.python, "rbn2_feeder.py", *src, *rx])
        return log

    # ---- radios ----

    def discover(self, seconds: float = DISCOVER_S, address: str | None = None,
                 kind: str | None = None, name: str | None = None) -> str:
        """The radios of one type on the local network (or the one at `address`), each written
        to its radio file (config/<name>.radio.json; the node settings stay); what was found and
        changed."""
        if name and (who := self.name_taken(name, own=name)):
            raise SeatError(f"{name} is already used by {who}")
        return radios.discover(self.root / "config", seconds, address, kind, name)

    def set_node(self, radio: str, callsign: str, grid: str, op_name: str) -> str:
        """A radio's node settings as sparkgap.sh setup sets them (radios.set_node); what changed,
        and which receivers take it at their next restart.  ValueError: refused, nothing written."""
        lines, receivers = radios.set_node(self.root / "config", radio, callsign, grid, op_name)
        restart = f"; restart {', '.join(receivers)} to use it" if receivers else ""
        return "; ".join(lines) + restart

    # ---- status ----

    def status(self, i: Instance) -> dict[str, Any]:
        with self._stats_lock:  # a log's counts are read on from where the last call stopped
            return self._status(i)

    def _status(self, i: Instance) -> dict[str, Any]:
        cfg = self.config(i)
        pid = self.pid(i.name)
        log = self.newest_log(i.name)
        stats = self._stats.setdefault(log, LogStats()) if log else LogStats()
        if log:
            stats.update(log)
            self._stats = {k: v for k, v in self._stats.items() if k.exists()}
        feed = cfg.get("gui_feed") if isinstance(cfg.get("gui_feed"), dict) else None
        # the port this run got, from its log: a configured port can be taken (the server's 7320)
        port = stats.feed_port if pid and feed and not stats.feed_failed else None
        errors = stats.errors
        out: dict[str, Any] = {
            "name": i.name,
            "state": "running" if pid else "stopped",
            "pid": pid,
            "band": self.band(i),
            "radio": self.radio(i) or (self.feeder_name(i) if self.radio_file(i) else None),
            "feed_port": port,
            "feed_failed": bool(pid and stats.feed_failed),
            "feed_code": feed.get("join_code") if feed else None,
            "feed_web": bool(feed and feed.get("web")),
            "telnet_port": cfg.get("telnet_port", 7300),
            "settings": self.settings(i),
            "spots": sum(stats.spots.values()),
            "top_calls": stats.spots.most_common(TOP_CALLS),
            "errors": stats.n_errors,
            "last_error": errors[-1] if errors and not pid else None,
            "warnings": stats.warnings,
            "reconnects": stats.reconnects,
            "log": log.name if log else None,
            "started": proc_started(pid) if pid else None,
            "build": self.build(i.code),
            "iq_trimmed": stats.iq_trimmed if pid else None,
            "receiver_list": self.receiver_list(i, cfg, stats),
            "rec_free_gb": (
                shutil.disk_usage(self.rec).free // 2**30
                if cfg.get("record_wav") and self.rec.exists()
                else None
            ),
        }
        return out

    @staticmethod
    def receiver_list(i: Instance, cfg: dict[str, Any], stats: LogStats) -> list[dict[str, Any]] | None:
        """An HPSDR radio's receivers, <radio>-1 on: one per bands[] entry, in order, with its spots
        from the log while its band is the one the log started on; None for any other copy."""
        if cfg.get("sdr_type") != "hpsdr":
            return None
        radio, half = cfg.get("radio") or i.name, float(cfg.get("sample_rate", 192_000)) / 2000
        out = []
        for n, b in enumerate(cfg.get("bands") or [], 1):
            band = str(b.get("name") or b.get("center_khz")) if isinstance(b, dict) else str(b)
            if isinstance(b, dict) and isinstance(b.get("rx_index"), int):  # sparkgap.py honours it
                n = b["rx_index"] + 1
            seen = stats.live.get(n - 1)
            centre = seen[1] if seen and seen[0] == band else None
            near = (lambda khz, c=centre: abs(khz - c) <= half) if centre is not None else None
            out.append({"name": f"{radio}-{n}", "receiver": n, "band": band, "centre_khz": centre,
                        "spots": sum(c for k, c in stats.spot_khz.items() if near(k)) if near else None,
                        "calls": len({c for k, c in stats.spot_calls if near(k)}) if near else None,
                        "windows": sum(c for k, c in stats.raw_khz.items() if near(k)) if near else None})
        return out

    @staticmethod
    def build(code: Path) -> str | None:
        """The commit a code folder has checked out, read from its git files: no subprocess
        in the status refresh, which runs every two seconds."""
        try:
            git = code / ".git"
            gitdir = git
            if git.is_file():  # a worktree: .git names its own git folder
                gitdir = Path(git.read_text().split("gitdir:", 1)[1].strip())
                gitdir = gitdir if gitdir.is_absolute() else (code / gitdir).resolve()
            head = (gitdir / "HEAD").read_text().strip()
            if not head.startswith("ref: "):
                return head[:7] or None
            ref = head[5:]
            common = gitdir
            if (gitdir / "commondir").exists():
                common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
            for d in (gitdir, common):
                if (d / ref).exists():
                    return (d / ref).read_text().strip()[:7] or None
            for line in (common / "packed-refs").read_text().splitlines():
                if line.endswith(" " + ref):
                    return line[:7]
        except (OSError, IndexError):
            return None
        return None

    def station_status(self, radio: str) -> dict[str, Any]:
        """Running or not; its health comes from its link (the server adds it)."""
        return station_view(radio, bool(self.station_pid(radio)), None)

    def radio_view(self, i: Instance) -> dict[str, Any]:
        """A radio as SparkGap Web and the command line show it: from its radio file."""
        st = self.node(i)
        caps = radios.capabilities(st.get("type"))
        return {
            "name": self.feeder_name(i),
            **{k: st.get(k) for k in ("type", "model", "address", "callsign", "grid", "op_name",
                                      "num_scu", "max_panadapters", "board_id", "receivers")},
            "rbn_upload": self.upload_mode(i),
            "rx_settings": list(caps.rx_settings),
        }

    def snapshot(self) -> dict[str, Any]:
        instances = self.instances()
        stations = sorted({r for i in instances if (r := self.radio(i))})
        nodes = {self.feeder_name(i): i for i in instances if self.radio_file(i)}
        return {
            "instances": [self.status(i) for i in instances],
            "stations": [self.station_status(r) for r in stations],
            "radios": [self.radio_view(i) for _, i in sorted(nodes.items())],
            "radio_files": radios.radio_files(self.root / "config"),  # every radio, for setup
            "bands": bands_in(CODE),  # what an added receiver can run
            "default_settings": self.default_settings(),
            "removed": self.removed_settings({i.name for i in instances}),
        }
