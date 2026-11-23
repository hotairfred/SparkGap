"""RBN's reference files, downloaded into rbn_known_dir as the Aggregator downloads them.

RBN's sign-in reply names them; both are public and fetched at most once a day, and a failed or
empty fetch keeps the previous copy:

- patt3ch.lst (PATT3CH_URL): CW Skimmer's call patterns, maintained for RBN by the Aggregator's
  author. The tracker reads it (2, 3 or 4 reads by pattern); without a download it reads the copy
  shipped in the checkout.
- bcn.csv (BCN_URL): every beacon RBN lists, `frequency,call` per line, fetched with the Gated
  Rule. Gate 12 reads it with spot_rule_beacons (cdub89/SparkGap#57).
- master.scp (MASTER_SCP_URL): Super Check Partial as RBN serves it, one call per line. The
  tracker reads it in place of the MASTER.SCP shipped in the checkout (cdub89/SparkGap#58 SC1).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

import downloads

PATT3CH_URL = "https://data.reversebeacon.net/downloads/patt3ch/patt3ch.lst"
BCN_URL = "https://data.reversebeacon.net/downloads/beacon/bcn.csv"
MASTER_SCP_URL = "https://data.reversebeacon.net/downloads/master/master.scp"
PATT3CH_FILE = "patt3ch.lst"
BCN_FILE = "bcn.csv"
MASTER_SCP_FILE = "master.scp"
MAX_AGE_S = 24 * 3600.0
MIN_PATTERNS = 10_000  # a download with fewer is not RBN's patt3ch.lst
MIN_BEACONS = 1_000    # a download with fewer rows is not RBN's beacon list
MIN_SCP_CALLS = 40_000  # a download with fewer calls is not RBN's master.scp
FETCH_TIMEOUT_S = 30.0
USER_AGENT = "SparkGap rbn_files"
PATTERN = re.compile(r"[+ ] +[0-9A-Z#@]+", re.IGNORECASE)  # "+ K1@@": + marks a common one
BEACON = re.compile(r"\d+,[A-Z0-9/-]+", re.IGNORECASE)
SCP_CALL = re.compile(r"[A-Z0-9/]+", re.IGNORECASE)


def _rows(path: Path, row: re.Pattern[str], least: int, what: str) -> int:
    """The file's row count; any row not of its form, or too few rows, is not the file."""
    rows = [r.rstrip() for r in path.read_text(errors="replace").splitlines() if r.strip()]
    bad = next((r for r in rows if not row.fullmatch(r)), None)
    if bad is not None:
        raise ValueError(f"not a {what} row: {bad[:40]!r}")
    if len(rows) < least:
        raise ValueError(f"{len(rows)} {what}s")
    return len(rows)


def _check_patt3ch(path: Path) -> int:
    return _rows(path, PATTERN, MIN_PATTERNS, "pattern")


def _check_beacons(path: Path) -> int:
    return _rows(path, BEACON, MIN_BEACONS, "beacon")


def _check_master_scp(path: Path) -> int:
    """master.scp's call count; its comment lines (# and !!Order) are not calls."""
    calls = [r.strip() for r in path.read_text(errors="replace").splitlines()
             if r.strip() and not r.startswith(("#", "!!"))]
    bad = next((c for c in calls if not SCP_CALL.fullmatch(c)), None)
    if bad is not None:
        raise ValueError(f"not a call row: {bad[:40]!r}")
    if len(calls) < MIN_SCP_CALLS:
        raise ValueError(f"{len(calls)} calls")
    return len(calls)


def _refresh_checked(path: Path, url: str, check: downloads.Check, what: str,
                     fetch: Callable[[], bytes] | None, now: Callable[[], float]) -> Path | None:
    """path fetched at most once a day; path, or None when no copy passes its check."""
    downloads.refresh(
        path, url, check, what, USER_AGENT, max_age_s=MAX_AGE_S,
        get=fetch or (lambda: downloads.fetch(url, USER_AGENT, FETCH_TIMEOUT_S)), now=now)
    try:
        check(path)
    except downloads.FAILED:
        return None
    return path


def refresh_master_scp(cache_dir: Path, fetch: Callable[[], bytes] | None = None,
                       now: Callable[[], float] = time.time) -> Path | None:
    """master.scp fetched at most once a day; its path, or None when no copy passes its check."""
    return _refresh_checked(cache_dir / MASTER_SCP_FILE, MASTER_SCP_URL, _check_master_scp,
                            "calls", fetch, now)


def refresh_patt3ch(cache_dir: Path, fetch: Callable[[], bytes] | None = None,
                    now: Callable[[], float] = time.time) -> Path | None:
    """patt3ch.lst fetched at most once a day; its path, or None when no copy passes its check."""
    return _refresh_checked(cache_dir / PATT3CH_FILE, PATT3CH_URL, _check_patt3ch, "patterns",
                            fetch, now)


def refresh_beacons(cache_dir: Path, fetch: Callable[[], bytes] | None = None,
                    now: Callable[[], float] = time.time) -> bool:
    """bcn.csv fetched at most once a day; whether a copy is there."""
    return downloads.refresh(
        cache_dir / BCN_FILE, BCN_URL, _check_beacons, "beacons", USER_AGENT, max_age_s=MAX_AGE_S,
        get=fetch or (lambda: downloads.fetch(BCN_URL, USER_AGENT, FETCH_TIMEOUT_S)), now=now)
