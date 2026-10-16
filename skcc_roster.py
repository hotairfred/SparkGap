"""SKCC member calls, for spot_validation_calls: "rbn+skcc".

The SKCC membership list (ROSTER_URL) is public and updated daily. Members key by hand (straight
key, bug, sideswiper), whose letter gaps the decoder misreads most, and many call CQ too seldom to
show in RBN's archives. The roster is fetched at most once a day into the cache directory; offline,
the cached copy is used.
"""

import logging
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROSTER_URL = "https://www.skccgroup.com/membership_data/skccdata.txt"
MAX_AGE_S = 24 * 3600.0
FETCH_TIMEOUT_S = 30.0
USER_AGENT = "SparkGap skcc_roster"

log = logging.getLogger("sparkgap")


def member_calls(text: str) -> set[str]:
    """Current member calls (CALL column, operator's call of a portable) from the roster text."""
    calls = set()
    for line in text.splitlines()[1:]:
        f = line.split("|")
        if len(f) > 1 and f[1].strip():
            calls.add(max(f[1].strip().upper().split("/"), key=len))
    return calls


def _fetch() -> bytes:
    req = urllib.request.Request(ROSTER_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        return resp.read()


def refresh(cache_dir: Path, fetch: Callable[[], bytes] = _fetch,
            now: Callable[[], float] = time.time) -> set[str]:
    """Member calls, from a roster fetched at most once a day; the cache when offline."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "skccdata.txt"
    if not path.exists() or now() - path.stat().st_mtime > MAX_AGE_S:
        try:
            data = fetch().decode("utf-8", errors="replace")
            if not data.startswith("SKCCNR|CALL|"):
                raise ValueError("not an SKCC roster")
            tmp = path.with_suffix(".tmp")
            tmp.write_text(data)
            tmp.replace(path)
        except Exception as e:                   # offline, refused, truncated or not a roster
            log.warning("skcc_roster: no roster download (%r); using the cache", e)
    calls = member_calls(path.read_text(errors="replace")) if path.exists() else set()
    log.info("skcc_roster: %d member calls", len(calls))
    return calls
