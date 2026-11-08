#!/usr/bin/env python3
"""rbn2_feeder.py: SparkGap's RBN feeder, authenticated by the private librbn2_auth library.

Reads SparkGap's local cluster telnet like rbn_feeder.py (whose spot parsing and band probe it
reuses) and posts to RBN's id.php and s.php. The two hash fields come from the
library librbn2_auth.so in the run root (the folder above this file's), handed out separately and
never kept in a git checkout.
Without a library of the expected version the feeder does not post (--dry-run aside).

Differences from rbn_feeder.py: heartbeat every 300 s;
SparkGap signs in as SKIM_VERSION; RBN lists it as skimmer SKIM_VERSION, Aggregator AGG_VERSION.

Identity and settings come from the SparkGap config; its rbn_upload key ("dry" or "live", off
when absent) decides whether it posts.  With --radio the feeder is its radio's node
(cdub89/SparkGap#33): identity and rbn_upload from the radio file, spots from every receiver the
server names, a call copied by two receivers posted once per re-spot interval.

usage: rbn2_feeder.py --config config/<seat>.local.json [--dry-run]
       rbn2_feeder.py --radio config/<radio>.radio.json --receiver <name> ... [--dry-run]
"""

import argparse
import ctypes
import hashlib
import http.client
import json
import logging
import math
import queue
import signal
import socket
import string
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlreq

import rbn_feeder as base
from process_link import ProcessLink

log = logging.getLogger("rbn2_feeder")

AUTH_VERSION = "rbn2auth/1.2"  # RBN2_AUTH_VERSION, compared as text
AUTH_LIB = "librbn2_auth.so"
SKIM_VERSION = "SparkGap 1.2"  # skimVersion and sign-in: names SparkGap in RBN's node list
AGG_VERSION = "6.7"  # id.php aggVersion and s.php agg, as other direct feeders send
UPLOAD_MODES = ("dry", "live")  # rbn_upload; absent or anything else is off
VALIDATION = {"permissive": "Permissive", "normal": "Normal", "aggressive": "Aggressive"}
VALIDATION_OFF = (None, False, "off", "")  # sparkgap.py's off values
UPLOAD_S = 10.0  # Aggregator's tick when id.php names no serverUploadInterval
VERSION_LEN = 64
HASH_LEN = 16
HASH_CHARS = 8
HEARTBEAT_S = 300.0
ID_RETRY_S = 60.0  # after a failed id.php; spots wait until it succeeds
HTTP_TIMEOUT_S = 15
READ_TIMEOUT_S = 60.0
RECONNECT_S = 5.0
STATUS_S = 5.0  # between status messages to the server's hub
RESPOT_S = 120.0  # sparkgap.py's respot_interval default
RESPOT_SLACK_S = 5.0  # a receiver's own re-spot reaches the feeder a little early or late
QSY_KHZ = 1.0  # sparkgap.py's re-spot rule: a call more than this away is a new spot
VALIDATION_ORDER = ("Off", "Permissive", "Normal", "Aggressive")
MODE_CODE = {"CW": "1", "RTTY": "11"}  # Aggregator 6.7's mode table
CQ_CODE = {"CQ": "1", "DX": "2", "BCN": "3", "BEACON": "3"}
HIDDEN = ("fingerPrint", "fp", "h", "shortHash", "skimSignIn")  # never in a log


def masked(body: dict[str, Any]) -> dict[str, Any]:
    """body with the HIDDEN fields replaced, for logging."""
    return {k: "<hidden>" if k in HIDDEN else v for k, v in body.items()}


class AuthError(RuntimeError):
    """The auth library is missing or not the version this feeder expects."""


class Auth:
    """librbn2_auth through ctypes: its version string and the hashes."""

    def __init__(self, path: str, loader: Callable[[str], Any] = ctypes.CDLL) -> None:
        try:
            lib = loader(path)
            ver, short, spot = (
                lib.rbn2_auth_version,
                lib.rbn2_auth_short_hash,
                lib.rbn2_auth_spot_hash,
            )
        except (OSError, AttributeError) as e:
            raise AuthError(f"cannot load {path}: {e}") from e
        ver.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        ver.restype = ctypes.c_int
        short.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
        spot.argtypes = [
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        short.restype = spot.restype = ctypes.c_int
        buf = ctypes.create_string_buffer(VERSION_LEN)
        ret = ver(buf, VERSION_LEN)
        if ret != 0 or buf.value != AUTH_VERSION.encode("ascii"):
            got = buf.value.decode("ascii", errors="replace")
            raise AuthError(
                f"{path} is version {got!r} (returned {ret}), this feeder takes {AUTH_VERSION}"
            )
        self.version = AUTH_VERSION
        self._short, self._spot = short, spot

    def _hash(self, fn: Any, *args: Any) -> str | None:
        out = ctypes.create_string_buffer(HASH_LEN)
        if fn(*args, out, HASH_LEN) != 0:
            return None
        h = out.value.decode("ascii", errors="replace")
        if len(h) != HASH_CHARS or not all(c in string.hexdigits for c in h):
            log.error("auth library returned a malformed hash: treated as a refusal")
            return None
        return h

    def short_hash(self, sign_in: str) -> str | None:
        return self._hash(self._short, sign_in.encode("utf-8"))

    def spot_hash(self, calls: list[str]) -> str | None:
        """h for a post of these calls, in row order; None when empty or refused."""
        if not calls:
            return None
        arr = (ctypes.c_char_p * len(calls))(*(c.encode("utf-8") for c in calls))
        return self._hash(self._spot, arr, len(calls))


def ntp_synced() -> bool:
    """The system clock is NTP-synchronised (timedatectl); False when that can't be read."""
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.stdout.strip() == "yes"


@dataclass
class Seat:
    """What the feeder takes from a seat config."""

    call: str
    grid: str
    name: str
    qth: str
    validation: str
    host: str
    port: int
    mode: str  # "dry" or "live"
    rtty: bool = False  # enable_rtty: the seat spots RTTY too, so its ranges go in rTTYBandLimits


def spotter_id(cfg: dict[str, Any]) -> str:
    """callsign plus skimmer_suffix, except the default "-#": the run log's RBN row spotter."""
    suffix = cfg.get("skimmer_suffix", "-#") or ""
    return (cfg.get("callsign") or "") + ("" if suffix == "-#" else suffix)


def seat_settings(cfg: dict[str, Any]) -> Seat:
    """The feeder's settings from a seat config; ValueError says why it can't feed."""
    mode = cfg.get("rbn_upload")
    if mode not in UPLOAD_MODES:
        raise ValueError(f"rbn_upload is {mode!r}: off")
    call, grid = spotter_id(cfg), cfg.get("grid")
    if not call or not grid:
        raise ValueError("callsign and grid must both be set")
    return Seat(
        call=call,
        grid=grid,
        name=cfg.get("op_name") or "",
        qth=cfg.get("qth") or "",
        validation=validation_of(cfg),
        host=cfg.get("telnet_host") or "127.0.0.1",
        port=int(cfg.get("telnet_port", 7300)),
        mode=mode,
        rtty=bool(cfg.get("enable_rtty", False)),
    )


def validation_of(cfg: dict[str, Any]) -> str:
    level = cfg.get("spot_validation")  # absent is off, as in sparkgap.py
    if level not in VALIDATION and level not in VALIDATION_OFF:
        raise ValueError(f"spot_validation is {level!r}: not a level sparkgap.py knows")
    return "Off" if level in VALIDATION_OFF else VALIDATION[level]


@dataclass
class Receiver:
    """One receiver the node feeds from: its telnet port and re-spot interval."""

    name: str
    port: int
    respot_s: float
    validation: str


def receiver_settings(config_dir: Path, name: str) -> Receiver:
    """A receiver's settings from config/<name>.local.json; ValueError says why not."""
    try:
        cfg = json.loads((config_dir / f"{name}.local.json").read_text())
    except (OSError, ValueError) as e:
        raise ValueError(f"receiver {name}: {e}") from None
    try:
        respot = float(cfg.get("respot_interval", RESPOT_S))
    except (TypeError, ValueError):
        respot = RESPOT_S
    if not math.isfinite(respot) or respot <= 0:
        respot = RESPOT_S
    return Receiver(name, int(cfg.get("telnet_port", 7300)), respot, validation_of(cfg))


def node_settings(radio: dict[str, Any], receivers: list[Receiver]) -> Seat:
    """The node's settings from its radio file and receivers; ValueError says why it can't feed.
    The validation sent is the least any receiver applies."""
    mode = radio.get("rbn_upload")
    if mode not in UPLOAD_MODES:
        raise ValueError(f"rbn_upload is {mode!r}: off")
    call, grid = radio.get("callsign") or "", radio.get("grid")
    if not call or not grid:
        raise ValueError("callsign and grid must both be set in the radio file")
    if not receivers:
        raise ValueError("no receiver to feed from")
    level = min((r.validation for r in receivers), key=VALIDATION_ORDER.index)
    return Seat(call=call, grid=grid, name=radio.get("op_name") or "", qth=radio.get("qth") or "",
                validation=level, host="127.0.0.1", port=receivers[0].port, mode=mode)


class Respot:
    """One post per call per re-spot interval across the node's receivers; a call more than
    QSY_KHZ away is a new spot, as in sparkgap.py.  A receiver's own re-spot (its skimmer keeps
    the interval) may come RESPOT_SLACK_S early; another receiver's copy waits the whole
    interval."""

    def __init__(self, interval_s: float = RESPOT_S) -> None:
        self.interval_s = interval_s
        self.last: dict[str, list[tuple[float, float, str]]] = {}

    def allow(self, call: str, freq: float, now: float, rx: str = "") -> bool:
        def holds(t: float, by: str) -> bool:
            wait = self.interval_s - (RESPOT_SLACK_S if by == rx else 0.0)
            return now - t < max(wait, 0.0)

        seen = [e for e in self.last.get(call, []) if now - e[0] < self.interval_s]
        if any(abs(f - freq) <= QSY_KHZ and holds(t, by) for t, f, by in seen):
            self.last[call] = seen
            return False
        self.last[call] = [*seen, (now, freq, rx)]
        return True


def auth_lib_path() -> Path:
    """librbn2_auth.so in the folder above this checkout, never inside it."""
    return Path(__file__).resolve().parent.parent / AUTH_LIB


def not_ready(auth: Auth | None, dry_run: bool) -> str | None:
    """Why the feeder must not post, or None. A dry run posts nothing, so it always may run."""
    if dry_run:
        return None
    if auth is None:
        return f"no {auth_lib_path()}: RBN would drop these spots"
    return None


class Session:
    """id.php and s.php for one node."""

    def __init__(
        self,
        *,
        url: str,
        call: str,
        name: str,
        grid: str,
        qth: str,
        validation: str,
        port: int,
        bands: list[tuple[float, float]],
        auth: Auth | None,
        version: str,
        fingerprint: str,
        dry_run: bool,
        bands_of: Callable[[], list[tuple[float, float]]] | None = None,
        rtty: bool = False,
    ) -> None:
        if auth is None and not dry_run:
            raise AuthError("no auth library: a session without one may only dry-run")
        self.url = url.rstrip("/")
        self.call, self.name, self.grid, self.qth = call, name, grid, qth
        self.validation, self.port, self.bands = validation, port, bands
        self.bands_of = bands_of  # read again before each id.php: a seat changes band while it runs
        self.rtty = rtty
        self.auth, self.version, self.fingerprint, self.dry_run = (
            auth,
            version,
            fingerprint,
            dry_run,
        )
        self.agg = AGG_VERSION
        self.upload_s = UPLOAD_S  # the server's serverUploadInterval once id.php names one
        self.registered = threading.Event()  # set while the last id.php succeeded
        self.counts = {"posts": 0, "sent": 0, "blacklisted": 0, "dropped": 0, "not signed in": 0}
        self.ntp = False  # the clock state the last id.php reported

    def sign_in(self) -> str:
        return f"{self.version} is operated by {self.name}, {self.call}"

    def id_body(self) -> dict[str, Any] | None:
        """The id.php body, or None when the sign-in could not be hashed."""
        bands = ",".join(f"{lo:.1f}-{hi:.1f}" for lo, hi in self.bands)
        sign_in = self.sign_in()
        short = self.auth.short_hash(sign_in) if self.auth else ""
        if short is None:
            log.error("auth library refused the sign-in: id.php not sent")
            return None
        self.ntp = ntp_synced()
        return {
            "ClockNtp": self.ntp,
            "ClockNtpDiff": 0,
            "SkimPort": str(self.port),
            "aggVersion": self.agg,
            # As Aggregator 6.7 (hotairfred/SparkGap#148): every source's limits in bandLimits, and again
            # in the field of each mode it spots; mIXEDBandLimits only for its "combine skimmers" option
            "bandLimits": bands,
            "cWBandLimits": bands,
            "fTBandLimits": "",
            "fingerPrint": self.fingerprint,
            "justCQ": "1",
            "mIXEDBandLimits": "",
            "masterSCPFilter": "0",
            "rTTYBandLimits": bands if self.rtty else "",
            "showIP": "1",
            "skimCall": self.call,
            "skimGrid": self.grid,
            "skimName": self.name,
            "skimQth": self.qth,
            "skimSignIn": sign_in,
            "skimValLevel": self.validation,
            "skimVersion": SKIM_VERSION,
            "shortHash": short,
            "t": "id",
        }

    def spots_body(self, spots: list[dict[str, Any]], now: float) -> dict[str, Any] | None:
        """The s.php body for these spots, or None when their calls can't be hashed (dropped)."""
        calls = [spot["call"] for spot in spots]
        h = self.auth.spot_hash(calls) if self.auth else ""
        if h is None:
            log.error("auth library refused calls %s: spots dropped", calls)
            return None
        rows = [
            [
                f"{spot['freq']:.2f}",
                spot["call"],
                CQ_CODE.get(spot["cq_flag"], "1"),
                str(spot["snr"]),
                str(spot["wpm"]),
                "dB",
                MODE_CODE.get(spot["mode"], "1"),
                spot["cq_flag"],
                "D:",
            ]
            for spot in spots
        ]
        return {
            "agg": self.agg,
            "e": self.call,
            "fp": self.fingerprint,
            "h": h,
            "nTP": self.ntp,
            "tm": now,
            "s": rows,
            "t": "s",
        }

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        data = json.dumps(body, separators=(",", ":"))
        if self.dry_run:
            log.info("[DRY] POST %s %s", path, json.dumps(masked(body), separators=(",", ":")))
            return None
        req = urlreq.Request(
            f"{self.url}/{path}",
            data=data.encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Connection": "Keep-Alive",
                "User-Agent": f"rbn2_feeder/{self.version}",
            },
        )
        try:
            with urlreq.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                got = json.loads(resp.read() or b"{}")
        except (urlerror.URLError, OSError, http.client.HTTPException) as e:
            log.warning("%s failed: %s", path, e)
            return None
        except ValueError:  # a reply that is not JSON text: the post itself went through
            return {}
        return got if isinstance(got, dict) else {}

    def post_id(self) -> bool:
        """Sign in (or heartbeat); spots go out only while the last one succeeded. The band list
        is read from the skimmer again first (cdub89/SparkGap#33 O1): a live band change moves
        the node's bandLimits, and RBN learns it at the next id.php, not at a restart."""
        if self.bands_of is not None:
            bands = self.bands_of()
            if bands and bands != self.bands:
                log.info("bands now %s (were %s)", bands, self.bands)
                self.bands = bands
            elif not bands and self.bands:
                log.warning("no band info from the skimmer's SETT: bandLimits kept as %s",
                            self.bands)
        body = self.id_body()
        resp = self.post("id.php", body) if body is not None else None
        ok = body is not None and (resp is not None or self.dry_run)
        if ok:
            self.registered.set()
        else:
            self.registered.clear()
        if resp:
            log.info("id.php reply: %s", masked(resp))
            interval = resp.get("serverUploadInterval")
            try:
                if interval is not None and float(interval) > 0:
                    self.upload_s = float(interval)
            except (TypeError, ValueError):
                log.warning(
                    "serverUploadInterval %r unusable: tick stays %.0f s", interval, self.upload_s
                )
        return ok


def upload_loop(
    sess: Session,
    spots: "queue.Queue[dict[str, Any]]",
    blacklist: set[str],
    shutdown: threading.Event,
    clock: Callable[[], float],
    respot: Respot | None = None,
) -> None:
    """Each upload tick, one post of every spot queued since the last (rbn_feeder.upload_loop);
    with respot, a call another receiver just spotted is dropped."""
    counts = sess.counts  # shared with the status the feeder sends the server
    while not shutdown.wait(sess.upload_s):
        batch: list[dict[str, Any]] = []
        while True:
            try:
                spot = spots.get_nowait()
            except queue.Empty:
                break
            if spot["call"] in blacklist:
                counts["blacklisted"] += 1
            elif respot is not None and not respot.allow(spot["call"], spot["freq"], clock(),
                                                         spot.get("rx", "")):
                counts["respot"] = counts.get("respot", 0) + 1
            else:
                batch.append(spot)
        if not batch:
            continue
        if not sess.registered.is_set():  # dropped, not held: no backlog of old spots later
            counts["not signed in"] += len(batch)
            continue
        body = sess.spots_body(batch, clock())
        if body is None or (sess.post("s.php", body) is None and not sess.dry_run):
            counts["dropped"] += len(batch)
        else:
            counts["sent"] += len(batch)
        counts["posts"] += 1
        log.info("%d spots: %s: %s", len(batch), " ".join(s["call"] for s in batch),
                 {k: v for k, v in counts.items() if k != "posts"})


def read_local(
    host: str,
    port: int,
    call: str,
    spots: queue.Queue[dict[str, Any]],
    shutdown: threading.Event,
    connect: Callable[..., socket.socket] = socket.create_connection,
) -> None:
    """SparkGap's telnet into spots. A quiet minute is not a lost connection: only EOF or a
    socket error reconnects, since a spot sent while disconnected never arrives."""
    while not shutdown.is_set():
        try:
            with connect((host, port), timeout=READ_TIMEOUT_S) as s:
                log.info("connected to local skimmer %s:%d", host, port)
                s.sendall(f"{call}\n".encode("ascii"))
                buf = b""
                while not shutdown.is_set():
                    try:
                        chunk = s.recv(8192)
                    except TimeoutError:
                        continue
                    if not chunk:
                        log.warning("local skimmer disconnected (EOF)")
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode("ascii", errors="replace").rstrip("\r")
                        if "DX de" in text:
                            spot = base.parse_spot(text)
                            if spot is not None and base.uploadable(spot):
                                spots.put(spot)
        except OSError as e:
            log.warning("local read error: %s: reconnecting in %.0f s", e, RECONNECT_S)
        if not shutdown.is_set():
            shutdown.wait(RECONNECT_S)


class Tagged:
    """A receiver's spots into the node's queue, each marked with the receiver."""

    def __init__(self, spots: "queue.Queue[dict[str, Any]]", rx: str) -> None:
        self.spots, self.rx = spots, rx

    def put(self, spot: dict[str, Any]) -> None:
        self.spots.put({**spot, "rx": self.rx})


class Readers:
    """One telnet reader per receiver of the node; set() follows the list the server sends."""

    def __init__(self, call: str, spots: "queue.Queue[dict[str, Any]]", shutdown: threading.Event,
                 failed: threading.Event, respot: Respot,
                 read: Callable[..., None] = read_local) -> None:
        self.call, self.spots, self.shutdown, self.failed = call, spots, shutdown, failed
        self.respot, self.read = respot, read
        self.receivers: dict[str, Receiver] = {}
        self.stops: dict[str, threading.Event] = {}
        self.lock = threading.Lock()

    def set(self, receivers: list[Receiver]) -> None:
        with self.lock:
            want = {r.name: r for r in receivers}
            for name in list(self.stops):
                if name not in want or want[name].port != self.receivers[name].port:
                    self.stops.pop(name).set()
                    del self.receivers[name]
                    log.info("receiver %s: no longer fed from", name)
            for name, r in want.items():
                if name not in self.stops:
                    stop = self.stops[name] = threading.Event()
                    self.receivers[name] = r
                    threading.Thread(target=self._run, args=(r, stop), name=f"read_{name}",
                                     daemon=True).start()
                    log.info("receiver %s: feeding from telnet :%d", name, r.port)
            if want:
                self.respot.interval_s = max(r.respot_s for r in want.values())

    def _run(self, r: Receiver, stop: threading.Event) -> None:
        try:
            self.read("127.0.0.1", r.port, self.call, Tagged(self.spots, r.name), stop)
        except Exception:
            log.exception("receiver %s: reader failed: the feeder stops", r.name)
            self.failed.set()
            self.shutdown.set()

    def ports(self) -> list[int]:
        with self.lock:
            return [r.port for r in self.receivers.values()]

    def names(self) -> list[str]:
        with self.lock:
            return sorted(self.receivers)

    def stop_all(self) -> None:
        self.set([])


def bands_of_ports(ports: list[int], call: str) -> list[tuple[float, float]]:
    """The band ranges every receiver's SETT names, in order, each once."""
    out: list[tuple[float, float]] = []
    for port in ports:
        out += [b for b in base.fetch_bands_from_skimmer("127.0.0.1", port, call) if b not in out]
    return out


def worker(target: Callable[..., None], shutdown: threading.Event, failed: threading.Event):
    """target in a thread that stops the feeder if it dies, so it never runs on without it."""

    def run(*args: Any) -> None:
        name = threading.current_thread().name
        try:
            target(*args)
        except Exception:
            log.exception("%s thread failed: the feeder stops", name)
        else:
            if shutdown.is_set():
                return
            log.error("%s thread ended unasked: the feeder stops", name)
        failed.set()
        shutdown.set()

    return run


def heartbeat_loop(sess: Session, shutdown: threading.Event) -> None:
    while not shutdown.wait(HEARTBEAT_S if sess.post_id() else ID_RETRY_S):
        pass


class LevelCount(logging.Handler):
    """Warnings and errors logged so far, for the status the server shows."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.warnings = self.errors = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            self.errors += 1
        else:
            self.warnings += 1


def status_loop(link: ProcessLink, sess: Session, seat: Seat, levels: LevelCount,
                shutdown: threading.Event, readers: Readers | None = None) -> None:
    """The feeder's status to the server every STATUS_S: mode, posts, spots, sign-in, bands."""
    while True:
        if readers is not None:
            link.status(receivers=readers.names())
        link.status(mode=seat.mode, spotter=sess.call, registered=sess.registered.is_set(),
                    bands=[list(b) for b in sess.bands], warnings=levels.warnings,
                    errors=levels.errors,
                    **{k.replace(" ", "_"): v for k, v in sess.counts.items()})
        if shutdown.wait(STATUS_S):
            return


def wait_for_stop(shutdown: threading.Event, failed: threading.Event) -> bool:
    """Runs until Ctrl-C, SIGTERM or a failed thread and logs how it stopped; True when clean."""
    by: list[str] = []

    def on_sigterm(_signum: int, _frame: Any) -> None:
        by.append("SIGTERM")
        shutdown.set()

    old = signal.signal(signal.SIGTERM, on_sigterm)
    try:
        while not shutdown.wait(1.0):
            pass
    except KeyboardInterrupt:
        by.append("Ctrl-C")
        shutdown.set()
    finally:
        signal.signal(signal.SIGTERM, old)
    if failed.is_set():
        log.error("stopped: a thread failed, see above")
        return False
    log.info("stopped (%s)", by[0] if by else "shutdown")
    return True


def main() -> None:
    p = argparse.ArgumentParser(description="SparkGap's RBN feeder (librbn2_auth)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--config", help="the seat config, config/<seat>.local.json")
    src.add_argument("--radio", help="the radio's file, config/<radio>.radio.json: its node")
    p.add_argument("--receiver", action="append", default=[],
                   help="with --radio: a receiver to feed from (config/<name>.local.json)")
    p.add_argument("--rbn-url", default=base.DEFAULT_RBN_URL)
    p.add_argument("--blacklist", default=str(Path(__file__).resolve().parent / "blacklist.txt"))
    p.add_argument("--dry-run", action="store_true", help="post nothing, whatever rbn_upload says")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    a = p.parse_args()
    logging.basicConfig(
        level=getattr(logging, a.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    levels = LevelCount()
    logging.getLogger().addHandler(levels)

    source = a.radio or a.config
    receivers: list[Receiver] = []
    try:
        cfg = json.loads(Path(source).read_text())
        if a.dry_run:
            cfg["rbn_upload"] = "dry"
        if a.radio:
            receivers = [receiver_settings(Path(a.radio).parent, n) for n in a.receiver]
            seat = node_settings(cfg, receivers)
        else:
            seat = seat_settings(cfg)
    except (OSError, ValueError) as e:
        sys.exit(f"rbn2_feeder: {source}: {e}")
    dry_run = seat.mode == "dry"

    auth = None
    lib = auth_lib_path()
    if lib.exists():
        try:
            auth = Auth(str(lib))
        except AuthError as e:
            sys.exit(f"rbn2_feeder: {e}")
        log.info("auth library %s from %s", auth.version, lib)
    why = not_ready(auth, dry_run)
    if why:
        sys.exit(f"rbn2_feeder: not posting: {why}")

    spots: queue.Queue[dict[str, Any]] = queue.Queue()
    shutdown, failed = threading.Event(), threading.Event()
    respot = Respot()
    readers = Readers(seat.call, spots, shutdown, failed, respot) if a.radio else None

    def bands_now() -> list[tuple[float, float]]:
        if readers is not None:
            return bands_of_ports(readers.ports() or [r.port for r in receivers], seat.call)
        return base.fetch_bands_from_skimmer(seat.host, seat.port, seat.call)

    bands = bands_now()
    if not bands:
        log.warning("no band info from the skimmer's SETT: empty bandLimits")
    fingerprint = hashlib.md5(f"{seat.call}@{socket.gethostname()}".encode()).hexdigest()
    sess = Session(
        url=a.rbn_url,
        call=seat.call,
        name=seat.name,
        grid=seat.grid,
        qth=seat.qth,
        validation=seat.validation,
        port=seat.port,
        bands=bands,
        auth=auth,
        version=SKIM_VERSION,
        fingerprint=fingerprint,
        dry_run=dry_run,
        bands_of=bands_now,
        rtty=seat.rtty,
    )
    mode = "dry run" if dry_run else "LIVE"
    log.info("SparkGap version %s, spotter %s, %s", sess.version, sess.call, mode)

    def on_command(cmd: str, args: dict[str, Any]) -> tuple[bool, str]:
        if cmd == "stop":
            log.info("stop asked by the server")
            shutdown.set()
            return True, "rbn feeder stopping"
        if cmd == "receivers" and readers is not None and a.radio:
            names = [str(n) for n in args.get("names") or []]
            try:
                readers.set([receiver_settings(Path(a.radio).parent, n) for n in names])
            except ValueError as e:
                return False, str(e)
            return True, f"rbn feeder: receivers {', '.join(names) or 'none'}"
        return False, f"unknown command {cmd!r}"

    # the server's view of this feeder (cdub89/SparkGap#33): linked while it runs; spots and
    # posts to RBN do not depend on it
    link = ProcessLink("feeder", Path(source).name.split(".")[0], on_command).start()
    threads = [
        threading.Thread(
            target=worker(upload_loop, shutdown, failed),
            args=(sess, spots, base.load_blacklist(a.blacklist), shutdown, time.time,
                  respot if readers is not None else None),
            name="upload",
            daemon=True,
        ),
        threading.Thread(
            target=worker(heartbeat_loop, shutdown, failed),
            args=(sess, shutdown),
            name="heartbeat",
            daemon=True,
        ),
        threading.Thread(
            target=status_loop, args=(link, sess, seat, levels, shutdown, readers), name="status",
            daemon=True
        ),
    ]
    if readers is None:
        threads.insert(0, threading.Thread(
            target=worker(read_local, shutdown, failed),
            args=(seat.host, seat.port, seat.call, spots, shutdown),
            name="read_local",
            daemon=True,
        ))
    else:
        readers.set(receivers)
    for t in threads:
        t.start()
    clean = wait_for_stop(shutdown, failed)
    if readers is not None:
        readers.stop_all()
    link.close()
    if not clean:
        sys.exit("rbn2_feeder: a thread failed, see the log")


if __name__ == "__main__":
    main()
