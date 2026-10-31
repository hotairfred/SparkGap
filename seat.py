"""The SparkGap instances on one machine: start, stop, status and settings.

The run folder (docs/SERVER_SETUP.md):
<root>/instances (name, code folder, config, python), config/<name>.local.json, logs/<name>.pid,
logs/<name>_<band>_<UTC>.log, logs/station_<radio>.{pid,json,lock}, rec/.  Every function blocks
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
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
RADIO_RE = re.compile(
    r"^[A-Za-z0-9.:-]{1,64}$"
)  # it goes into file names: a host name or address only
VALIDATION = ("off", "permissive", "normal", "aggressive")
CALL_SOURCES = ("scp", "rbn", "rbn+skcc")
# decoder options by config key: on/off ones, and ones with a few values (None: off, key removed)
OPTION_FLAGS = (
    "spot_rule_split_runs",
    "spot_rule_contest_words",
    "spot_rule_cq_repeats",
    "spot_rule_doubled_calls",
    "spot_rule_split_up",
    "spot_rule_runner_signs",
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
# a Flex copy's receive path: setting -> (the copy's live key, its config key).  The radio's
# state is the truth: a running copy changes it live; startup values change while it is stopped
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


class SeatError(RuntimeError):
    """A request the seat refuses or could not carry out; the message says why."""


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
        """The Flex radio an instance uses (its sdr_ip), None for any other receiver."""
        cfg = self.config(i)
        if cfg.get("sdr_type") != "flex" or not cfg.get("sdr_ip"):
            return None
        radio = str(cfg["sdr_ip"])
        if not RADIO_RE.match(radio):
            raise SeatError(f"{i.name}: sdr_ip {radio!r} is not a host name or address")
        return radio

    @staticmethod
    def bands_known(i: Instance) -> list[str]:
        """Band names the instance's sparkgap.py accepts, from its BANDS line."""
        def parse(text: str) -> list[str]:
            m = re.search(r"^BANDS = dict\(zip\((.*)$", text, re.M)
            return re.findall(r"'(\d+m)'", m.group(1)) if m else []

        try:
            return list(cached(i.code / "sparkgap.py", parse))
        except OSError:
            return []

    # ---- process files ----

    def pid(self, name: str) -> int | None:
        return self._alive(self.logs / f"{name}.pid", "sparkgap.py")

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
                "--status",
                str(self.logs / f"station_{radio}.json"),
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
            "validation": cfg.get("spot_validation") or "off",
            "call_source": cfg.get("spot_validation_calls") or "scp",
            "afc": bool(cfg.get("itila2_afc", False)),
            "decoder": cfg.get("cw_decoder", "itila2"),  # shown only: AFC acts with itila2
            **{k: bool(cfg.get(k, False)) for k in OPTION_FLAGS},
            **{k: cfg.get(k, OPTION_DEFAULTS.get(k)) for k in OPTION_CHOICES},
            "record": bool(cfg.get("record_wav")),
            # a Flex copy's receive path as its config sets it at start (None: the radio's own)
            **{k: cfg.get(c) for k, (_, c) in RX_SETTINGS.items()},
        }

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
        Receive path settings (RX_SETTINGS) on a running copy go to live_rx(name, change) only: the
        config holds startup values, written while the copy is stopped."""
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

    def _check_rx(self, i: Instance, key: str, value: Any) -> None:
        if self.radio(i) is None:
            raise SeatError(f"{key} is a FlexRadio setting")
        if key == "antenna" and not (isinstance(value, str) and ANTENNA_RE.match(value)):
            raise SeatError("antenna must be one the radio lists")
        if key in ("loop_a", "loop_b") and not isinstance(value, bool):
            raise SeatError(f"{key} must be true or false")
        if key == "rf_gain" and (isinstance(value, bool) or not isinstance(value, int)):
            raise SeatError("rf_gain must be a whole number of dB")

    def startup(self, name: str, settings: dict[str, Any]) -> str:
        """Write receive path startup values (RX_SETTINGS) to name's config: the radio and a
        running copy are left as they are."""
        i = self.instance(name)
        if not settings or not set(settings) <= set(RX_SETTINGS):
            raise SeatError(f"startup values here are: {', '.join(RX_SETTINGS)}")
        for key, value in settings.items():
            self._check_rx(i, key, value)
        with self.lock(i):
            write_config(i.config, {RX_SETTINGS[k][1]: v for k, v in settings.items()})
        return f"{name}: starts with {', '.join(f'{k} {v}' for k, v in settings.items())}"

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
            "radio": self.radio(i),
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
            "rec_free_gb": (
                shutil.disk_usage(self.rec).free // 2**30
                if cfg.get("record_wav") and self.rec.exists()
                else None
            ),
        }
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
        out: dict[str, Any] = {
            "radio": radio,
            "state": "running" if self.station_pid(radio) else "stopped",
        }
        try:
            health = json.loads((self.logs / f"station_{radio}.json").read_text())
        except (OSError, ValueError):
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

    def snapshot(self) -> dict[str, Any]:
        instances = self.instances()
        radios = sorted({r for i in instances if (r := self.radio(i))})
        return {
            "instances": [self.status(i) for i in instances],
            "stations": [self.station_status(r) for r in radios],
        }
