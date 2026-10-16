"""Known calls from the RBN daily archives, for spot_validation_calls: "rbn".

A call is known when, on any of the last `days` days, MIN_SKIMMERS or more other skimmers spotted it
in CW. The archives (https://data.reversebeacon.net/rbn_history/YYYYMMDD.zip) are public files and
need no login. Our own spotter's spots never count, so a call we bust cannot vouch for itself,
and an operator's nodes (W1NT-2, W1NT-6) count as one skimmer.
Each day is reduced to call counts once and cached; a day not yet published is tried at the next
start. Downloads are spaced FETCH_PAUSE_S apart, so a first start with a long window takes a while
(365 days: about an hour); run it alone first: python rbn_known.py --callsign WX7V --days 365
"""

import argparse
import csv
import io
import logging
import re
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ARCHIVE_URL = "https://data.reversebeacon.net/rbn_history/{day}.zip"
DAYS = 7                      # default days of archives a call can be known from (rbn_known_days)
MIN_SKIMMERS = 10             # distinct other skimmers on one day
KEEP_DAYS = 30                # cached days older than this (and than the window) are deleted
FETCH_TIMEOUT_S = 30.0
FETCH_PAUSE_S = 10.0          # between downloads: polite to RBN
USER_AGENT = "SparkGap rbn_known"
CACHE_VERSION = 2             # in cached day names; 2: nodes of one operator count once

log = logging.getLogger("sparkgap")


def base_call(call: str) -> str:
    """The operator's call in a portable call: the longest part (PJ2/AG3I, AG3I/P -> AG3I)."""
    return max(call.upper().split("/"), key=len)


def spotter(call: str) -> str:
    """The operator behind a skimmer node: WF8Z-2, WF8Z-# and WX7V/5 -> WF8Z, WF8Z, WX7V."""
    return base_call(re.sub(r"-(\d+|#)$", "", call.strip()))


def day_counts(rows: Iterable[dict[str, str]], own_call: str) -> Counter[str]:
    """Distinct CW skimmer operators per spotted base call in one day's rows, our own left out."""
    own = spotter(own_call) if own_call else None
    skimmers: dict[str, set[str]] = {}
    for r in rows:
        if r.get("tx_mode") != "CW" or not r.get("dx") or not r.get("callsign"):
            continue
        op = spotter(r["callsign"])
        if op == own:
            continue
        skimmers.setdefault(base_call(r["dx"]), set()).add(op)
    return Counter({c: len(s) for c, s in skimmers.items()})


def read_archive(data: bytes) -> Iterable[dict[str, str]]:
    """Rows of one RBN daily archive zip (one CSV inside)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"))


def known_calls(days: Iterable[Counter[str]], min_skimmers: int = MIN_SKIMMERS) -> set[str]:
    """Calls with min_skimmers or more distinct skimmers on at least one of the days."""
    return {c for counts in days for c, n in counts.items() if n >= min_skimmers}


def _fetch(day: date) -> bytes:
    url = ARCHIVE_URL.format(day=f"{day:%Y%m%d}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        return resp.read()


def _load(path: Path) -> Counter[str] | None:
    """One cached day, or None when the file is damaged (it is then fetched again)."""
    counts: Counter[str] = Counter()
    try:
        for line in path.read_text().splitlines():
            call, n = line.split("\t")
            counts[call] = int(n)
    except (OSError, ValueError):
        return None
    return counts


def _write(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)                     # a crash mid-write never leaves a short day


def refresh(cache_dir: Path, own_call: str, days: int = DAYS, today: date | None = None,
            fetch: Callable[[date], bytes] = _fetch,
            sleep: Callable[[float], None] = time.sleep) -> set[str]:
    """Fetch any missing day of the last DAYS (yesterday back); write and return the known calls."""
    today = today or datetime.now(timezone.utc).date()
    cache_dir.mkdir(parents=True, exist_ok=True)
    tag = spotter(own_call) if own_call else "ALL"
    counted: list[Counter[str]] = []
    have: list[date] = []
    offline = False
    fetched = 0
    for back in range(1, days + 1):
        day = today - timedelta(days=back)
        path = cache_dir / f"{day:%Y%m%d}_{tag}_v{CACHE_VERSION}.calls"
        counts = _load(path) if path.exists() else None
        if counts is None:
            if offline:
                continue
            if fetched:
                sleep(FETCH_PAUSE_S)
            fetched += 1
            try:
                counts = day_counts(read_archive(fetch(day)), own_call)
            except urllib.error.HTTPError as e:
                if e.code == 404:                    # not published yet
                    log.warning("rbn_known: no archive for %s (%s)", day, e)
                    continue
                log.warning("rbn_known: archive for %s refused (%s); using the cache only", day, e)
                offline = True                       # RBN refusing: no more tries this start
                continue
            except OSError as e:
                log.warning("rbn_known: no archive for %s (%s); using the cache only", day, e)
                offline = True                       # one network failure: no more tries this start
                continue
            except Exception as e:                   # truncated or damaged download
                log.warning("rbn_known: unreadable archive for %s (%r)", day, e)
                continue
            _write(path, "".join(f"{c}\t{n}\n" for c, n in sorted(counts.items())))
        counted.append(counts)
        have.append(day)
    for old in cache_dir.glob("*.calls"):
        stamp = old.name[:8]
        try:
            keep = timedelta(days=max(KEEP_DAYS, days))
            if datetime.strptime(stamp, "%Y%m%d").date() < today - keep:
                old.unlink()
        except ValueError:
            continue
    known = known_calls(counted)
    span = f"{min(have):%Y-%m-%d} to {max(have):%Y-%m-%d}" if have else "none"
    _write(cache_dir / "rbn_known.txt",
        f"# RBN known calls: {MIN_SKIMMERS}+ skimmers on one day, {span}, "
        f"own spotter {tag} left out\n"
        + "".join(f"{c}\n" for c in sorted(known)))
    log.info("rbn_known: %d known calls from %d days (%s)", len(known), len(have), span)
    return known


def main() -> None:
    p = argparse.ArgumentParser(description="Refresh the RBN known-calls cache.")
    p.add_argument("--cache-dir", type=Path, default=Path("rbn_known"))
    p.add_argument("--callsign", default="", help="our spotter call, left out of the counts")
    p.add_argument("--days", type=int, default=DAYS, help="days of archives (rbn_known_days)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(len(refresh(a.cache_dir, a.callsign, a.days)))


if __name__ == "__main__":
    main()
