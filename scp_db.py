"""Super Check Partial calls (SCP.DB), for spot_rule_scp.

SCP.DB (SCP_URL) is the SQLite form of Super Check Partial: every call seen in the contest logs it
is built from, about 69,000, where MASTER.SCP keeps the 50,000 most active. It is fetched when
SparkGap starts, at most once a day, into the cache directory; offline, the cached copy is used.
"""

import logging
import os
import sqlite3
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

SCP_URL = "https://www.supercheckpartial.com/SCP.DB"
FILE_NAME = "SCP.DB"
MAX_AGE_S = 24 * 3600.0
MIN_CALLS = 20_000              # a download with fewer is not SCP.DB, and never replaces the cache
FETCH_TIMEOUT_S = 60.0
USER_AGENT = "SparkGap scp_db"

log = logging.getLogger("sparkgap")


def calls(path: Path) -> frozenset[str]:
    """Every call in an SCP.DB file (its callsigns table), read-only."""
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"          # escapes a path with spaces, # or ?
    con = sqlite3.connect(uri, uri=True)
    try:
        return frozenset(c.upper() for (c,) in con.execute("SELECT callsign FROM callsigns"))
    finally:
        con.close()


def _fetch() -> bytes:
    req = urllib.request.Request(SCP_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        return resp.read()


def _write(path: Path, data: bytes) -> None:
    """Write through a temp file of this writer's own, and keep it only when it reads as SCP.DB."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        n = len(calls(tmp))
        if n < MIN_CALLS:
            raise ValueError(f"{n} calls")
        tmp.replace(path)                 # a crash or a bad download never replaces the cache
    finally:
        tmp.unlink(missing_ok=True)


def refresh(cache_dir: Path, fetch: Callable[[], bytes] | None = None,
            now: Callable[[], float] = time.time) -> frozenset[str]:
    """SCP calls, from SCP.DB fetched at most once a day; the cache when offline; else empty."""
    fetch = fetch or _fetch
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / FILE_NAME
    if not path.exists() or now() - path.stat().st_mtime > MAX_AGE_S:
        try:
            _write(path, fetch())
        except Exception as e:                   # offline, refused, truncated or not SCP.DB
            log.warning("scp_db: no SCP.DB download (%r); using the cache", e)
    try:
        found = calls(path) if path.exists() else frozenset()
    except sqlite3.Error as e:
        log.warning("scp_db: %s unreadable (%r)", path, e)
        found = frozenset()
    log.info("scp_db: %d SCP calls", len(found))
    return found
