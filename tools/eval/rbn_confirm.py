#!/usr/bin/env python3
"""Check spotted calls against the RBN daily archive.

A spotted call is confirmed when another RBN skimmer spotted the same call within
--tol-khz of our frequency between --start and --end (widened by --slack-min).
Unconfirmed spots are junk candidates: real stations nobody else heard, or busts.

Spots are read from a SparkGap log (file-mode "SPOT: 7031.2 kHz CALL ..." or live
"*** SPOT:  7031.2  CALL ...") or a DX-cluster tee ("DX de X:  7031.2  CALL ...").
The archive (data.reversebeacon.net/rbn_history/YYYYMMDD.zip) is fetched once per
day into --archive-dir; it is posted the day after.

USAGE
  rbn_confirm.py LOG --start 2026-03-19T03:15:00 --end 2026-03-19T03:30:00
  rbn_confirm.py LOG --start ... --end ... --exclude-spotter WF8Z --show-confirmed
"""

import argparse
import csv
import io
import re
import sys
import urllib.request
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ARCHIVE_URL = "https://data.reversebeacon.net/rbn_history/{day}.zip"
USER_AGENT = "SparkGap-eval rbn_confirm.py"  # the archive returns 403 to urllib's default
UTC = timezone.utc  # datetime.UTC needs Python 3.11
DEFAULT_TOL_KHZ = 1.0
DEFAULT_SLACK_MIN = 10

_SPOT_RE = re.compile(r"(?:SPOT:|DX de \S+:)\s+([\d.]+)\s+(?:kHz\s+)?([A-Z0-9/]+)", re.I)


@dataclass(frozen=True)
class RbnSpot:
    spotter: str
    dx: str
    freq_khz: float
    time: datetime


def base_call(call: str) -> str:
    """The part of a slashed call that holds the callsign (W1AW/4 -> W1AW, EA8/DL1ABC -> DL1ABC)."""
    parts = [p for p in call.upper().split("/") if p]
    with_digit = [p for p in parts
                  if any(ch.isdigit() for ch in p) and any(ch.isalpha() for ch in p)]
    return max(with_digit or parts or [call.upper()], key=len)


def parse_spots(lines: Iterable[str]) -> dict[str, set[float]]:
    """Spotted base call -> frequencies (kHz, 0.1 kHz resolution)."""
    spots: dict[str, set[float]] = {}
    for line in lines:
        m = _SPOT_RE.search(line)
        if m:
            spots.setdefault(base_call(m.group(2)), set()).add(round(float(m.group(1)), 1))
    return spots


def parse_archive(rows: Iterable[dict[str, str]]) -> Iterator[RbnSpot]:
    for row in rows:
        if row.get("tx_mode") != "CW":
            continue
        try:
            t = datetime.strptime(row["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            f = float(row["freq"])
        except (KeyError, ValueError):
            continue
        yield RbnSpot(row["callsign"].upper(), base_call(row["dx"]), f, t)


def load_day(day: str, archive_dir: Path) -> list[RbnSpot]:
    """One day of the archive, fetched to archive_dir on first use."""
    path = archive_dir / f"{day}.zip"
    if not path.exists():
        archive_dir.mkdir(parents=True, exist_ok=True)
        url = ARCHIVE_URL.format(day=day)
        print(f"fetching {url}", file=sys.stderr)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req) as resp:
            path.write_bytes(resp.read())
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as fh:
            text = io.TextIOWrapper(fh, "utf-8", errors="replace")
            return list(parse_archive(csv.DictReader(text)))


def confirm(spots: dict[str, set[float]], archive: Iterable[RbnSpot],
            start: datetime, end: datetime,
            tol_khz: float = DEFAULT_TOL_KHZ,
            exclude: tuple[str, ...] = ()) -> dict[str, set[str]]:
    """Spotted call -> the other skimmers that heard it within tol_khz in [start, end]."""
    heard: dict[str, set[str]] = {c: set() for c in spots}
    for s in archive:
        if s.dx not in spots or not start <= s.time <= end:
            continue
        if any(s.spotter.startswith(e.upper()) for e in exclude):
            continue
        if any(abs(s.freq_khz - f) <= tol_khz for f in spots[s.dx]):
            heard[s.dx].add(s.spotter)
    return heard


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.rstrip("Z")).replace(tzinfo=UTC)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", type=Path, help="SparkGap log or DX-cluster tee holding the spots")
    ap.add_argument("--start", type=_utc, required=True, help="UTC start of the recording or run")
    ap.add_argument("--end", type=_utc, required=True, help="UTC end of the recording or run")
    ap.add_argument("--slack-min", type=float, default=DEFAULT_SLACK_MIN,
                    help="widen the window each side")
    ap.add_argument("--tol-khz", type=float, default=DEFAULT_TOL_KHZ)
    ap.add_argument("--exclude-spotter", action="append", default=[],
                    help="spotter callsign prefix to ignore (our own node); repeatable")
    ap.add_argument("--archive-dir", type=Path, default=Path("/tmp/rbn_history"))
    ap.add_argument("--show-confirmed", action="store_true", help="also list the confirmed calls")
    a = ap.parse_args()

    spots = parse_spots(a.log.read_text(errors="replace").splitlines())
    slack = timedelta(minutes=a.slack_min)
    t0, t1 = a.start - slack, a.end + slack
    days = sorted({(t0 + timedelta(days=k)).strftime("%Y%m%d") for k in range((t1 - t0).days + 1)}
                  | {t1.strftime("%Y%m%d")})
    archive = [s for d in days for s in load_day(d, a.archive_dir)]
    heard = confirm(spots, archive, t0, t1, a.tol_khz, tuple(a.exclude_spotter))

    confirmed = sorted(c for c, sk in heard.items() if sk)
    unconfirmed = sorted(c for c, sk in heard.items() if not sk)
    n = len(heard)
    pct = 100.0 * len(confirmed) / n if n else 0.0
    print(f"{a.log.name}: {n} spotted calls | confirmed {len(confirmed)} ({pct:.0f}%) | "
          f"unconfirmed {len(unconfirmed)}")
    print("unconfirmed: " + " ".join(f"{c}@{'/'.join(f'{f:.1f}' for f in sorted(spots[c]))}"
                                     for c in unconfirmed))
    if a.show_confirmed:
        for c in confirmed:
            print(f"  {c:<10} {len(heard[c]):>3} skimmers: {' '.join(sorted(heard[c])[:6])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
