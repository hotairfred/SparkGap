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
RULE_FLAGS = ("spot_rule_split_runs", "spot_rule_contest_words", "spot_rule_cq_repeats")
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


def write_config(path: Path, changes: dict[str, Any]) -> None:
    """Set keys in a JSON config (None removes one), keeping its indentation; one atomic replace."""
    text = path.read_text()
    m = re.search(r'\n([ \t]+)"', text)
    indent: int | str = m.group(1) if m else 2
    cfg = json.loads(text)
    for key, value in changes.items():
        if value is None:
            cfg.pop(key, None)
        else:
            cfg[key] = value
    new = path.with_name(path.name + ".new")
    new.write_text(json.dumps(cfg, indent=indent) + "\n")
    os.replace(new, path)


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
        cfg: Any = json.loads(i.config.read_text())
        return cfg if isinstance(cfg, dict) else {}

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
        try:
            text = (i.code / "sparkgap.py").read_text(errors="replace")
        except OSError:
            return []
        m = re.search(r"^BANDS = dict\(zip\((.*)$", text, re.M)
        return re.findall(r"'(\d+m)'", m.group(1)) if m else []

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
            "validation": cfg.get("spot_validation", "normal") or "off",
            "call_source": cfg.get("spot_validation_calls") or "scp",
            **{k: bool(cfg.get(k, False)) for k in RULE_FLAGS},
            "record": bool(cfg.get("record_wav")),
        }

    def change(
        self,
        name: str,
        settings: dict[str, Any],
        live: Callable[[str, str], str | None] | None = None,
    ) -> str:
        """Check every setting, write them all at once, restart the copy if it runs. A band change
        alone first goes to live(name, band), which moves the running copy in place and returns its
        answer, or None when it could not (then the copy restarts, onto the band just written)."""
        i = self.instance(name)
        changes: dict[str, Any] = {}
        for key, value in settings.items():
            if key == "band":
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
            elif key in RULE_FLAGS:
                if not isinstance(value, bool):
                    raise SeatError(f"{key} must be true or false")
                changes[key] = value
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
        if not changes:
            raise SeatError("nothing to change")
        done = f"{name}: {', '.join(f'{k} {v}' for k, v in settings.items())}"
        with self.lock(i):  # the write and its restart as one step: nothing slips in between
            write_config(i.config, changes)
            if not self.pid(name):
                return done
            if live is not None and set(settings) == {"band"}:
                answer = live(name, settings["band"])
                if answer:
                    return f"{name}: {answer}"
            self._stop(i)
            return f"{done}; {self._start(i)}"

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
