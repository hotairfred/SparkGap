"""SKCC member calls, for spot_validation_calls: "rbn+skcc".

The SKCC membership list (ROSTER_URL) is public and updated daily. Members key by hand (straight
key, bug, sideswiper), whose letter gaps the decoder misreads most, and many call CQ too seldom to
show in RBN's archives. The roster is fetched at most once a day into the cache directory; offline,
the cached copy is used.
"""

import logging
import time
from collections.abc import Callable
from pathlib import Path

import downloads

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
    return downloads.fetch(ROSTER_URL, USER_AGENT, FETCH_TIMEOUT_S)


def _write(path: Path, text: str) -> None:
    """Write through a temp file of this writer's own: copies sharing the cache never collide."""
    downloads.write_atomic(path, text.encode())


def _check(path: Path) -> int:
    """The roster's member count; a file without the roster's header is not the roster."""
    text = path.read_text(errors="replace")
    if not text.startswith("SKCCNR|CALL|"):
        raise ValueError("not an SKCC roster")
    return len(member_calls(text))


def refresh(cache_dir: Path, fetch: Callable[[], bytes] | None = None,
            now: Callable[[], float] = time.time) -> set[str]:
    """Member calls, from a roster fetched at most once a day; the cache when offline."""
    path = cache_dir / "skccdata.txt"
    downloads.refresh(path, ROSTER_URL, _check, "members", USER_AGENT, max_age_s=MAX_AGE_S,
                      get=fetch or _fetch, now=now)
    calls = member_calls(path.read_text(errors="replace")) if path.exists() else set()
    log.info("skcc_roster: %d member calls", len(calls))
    return calls
