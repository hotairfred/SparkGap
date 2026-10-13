#!/usr/bin/env python3
"""Check spotted calls against the RBN daily archive.

A spotted call is confirmed when another RBN skimmer spotted the same call within
--tol-khz of our frequency between --start and --end (widened by --slack-min).
Unconfirmed spots are junk candidates: real stations nobody else heard, or busts.

Spots are read from a SparkGap log (file-mode "SPOT: 7031.2 kHz CALL ..." or live
"*** SPOT:  7031.2  CALL ...") or a DX-cluster tee ("DX de X:  7031.2  CALL ...").
The archive (data.reversebeacon.net/rbn_history/YYYYMMDD.zip) is fetched once per
day into --archive-dir; it is posted the day after.

Nearby skimmers (--near GRID): a skimmer 2,000 miles away often can't hear what
we hear, so "nobody confirmed it" partly means nobody nearby was listening. With
--near, only skimmers within --radius-mi of GRID count (locations from RBN's node
status page, cached in --archive-dir for a day), and a second figure is printed:
NEARBY RECALL, the calls that at least --min-nearby nearby skimmers spotted on our
bands during --start..--end, and how many of them we spotted within --tol-khz.
RBN only carries calls that passed its spotting rules (CQ/TEST, repeats), so nearby
recall compares like with like; precision still compares our raw spots with
filtered ones and reads low.

USAGE
  rbn_confirm.py LOG --start 2026-03-19T03:15:00 --end 2026-03-19T03:30:00
  rbn_confirm.py LOG --start ... --end ... --exclude-spotter WF8Z --show-confirmed
  rbn_confirm.py LOG --start ... --end ... --exclude-spotter WF8Z --near EM79SM
"""

import argparse
import csv
import io
import json
import math
import re
import sys
import time
import urllib.request
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ARCHIVE_URL = "https://data.reversebeacon.net/rbn_history/{day}.zip"
NODES_URL = "https://www.reversebeacon.net/cont_includes/status.php?t=skt"
USER_AGENT = "SparkGap-eval rbn_confirm.py"  # the archive returns 403 to urllib's default
UTC = timezone.utc  # datetime.UTC needs Python 3.11
DEFAULT_TOL_KHZ = 1.0
DEFAULT_SLACK_MIN = 10
DEFAULT_RADIUS_MI = 300   # Grayline's HF local-spotter radius
DEFAULT_MIN_NEARBY = 1

# Amateur band edges (kHz) for restricting nearby recall to the bands we cover.
BANDS = {"160m": (1800, 2000), "80m": (3500, 4000), "60m": (5330, 5410),
         "40m": (7000, 7300), "30m": (10100, 10150), "20m": (14000, 14350),
         "17m": (18068, 18168), "15m": (21000, 21450), "12m": (24890, 24990),
         "10m": (28000, 29700), "6m": (50000, 54000)}

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
            exclude: tuple[str, ...] = (),
            only: set[str] | None = None) -> dict[str, set[str]]:
    """Spotted call -> the other skimmers that heard it within tol_khz in [start, end].
    only: if given, count just these spotters (e.g. the nearby ones)."""
    heard: dict[str, set[str]] = {c: set() for c in spots}
    for s in archive:
        if s.dx not in spots or not start <= s.time <= end:
            continue
        if any(s.spotter.startswith(e.upper()) for e in exclude):
            continue
        if only is not None and s.spotter not in only:
            continue
        if any(abs(s.freq_khz - f) <= tol_khz for f in spots[s.dx]):
            heard[s.dx].add(s.spotter)
    return heard


def band_of(freq_khz: float) -> str | None:
    for name, (lo, hi) in BANDS.items():
        if lo <= freq_khz <= hi:
            return name
    return None


# Maidenhead and great-circle distance, as in Grayline (same author, BSD-3).
def grid_latlon(grid: str) -> tuple[float, float] | None:
    """Centre of a 4- or 6-character Maidenhead square; None if invalid."""
    g = grid.strip()
    if len(g) < 4 or not ("A" <= g[0].upper() <= "R" and "A" <= g[1].upper() <= "R"
                          and g[2].isdigit() and g[3].isdigit()):
        return None
    lon = (ord(g[0].upper()) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1].upper()) - 65) * 10 - 90 + int(g[3])
    if len(g) >= 6 and "a" <= g[4].lower() <= "x" and "a" <= g[5].lower() <= "x":
        lon += (ord(g[4].lower()) - 97) * 5 / 60 + 2.5 / 60
        lat += (ord(g[5].lower()) - 97) * 2.5 / 60 + 1.25 / 60
    else:
        lon += 1
        lat += 0.5
    return lat, lon


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(h))


def parse_nodes(html: str) -> dict[str, str]:
    """RBN node status page -> {skimmer callsign: grid}."""
    nodes: dict[str, str] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        m = re.search(r"c=([A-Z0-9/-]+)&(?:amp;)?t=de", row)
        g = re.findall(r"<td>\s*([A-R]{2}[0-9]{2}(?:[a-xA-X]{2})?)\s*</td>", row)
        if m and g:
            nodes[m.group(1).upper()] = g[0]
    return nodes


def load_nodes(archive_dir: Path, max_age_s: float = 86400) -> dict[str, str]:
    """Skimmer grids from RBN's node status page, cached in archive_dir for a day."""
    path = archive_dir / "rbn_nodes.json"
    if not path.exists() or time.time() - path.stat().st_mtime > max_age_s:
        archive_dir.mkdir(parents=True, exist_ok=True)
        print(f"fetching {NODES_URL}", file=sys.stderr)
        req = urllib.request.Request(NODES_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req) as resp:
            nodes = parse_nodes(resp.read().decode("utf-8", errors="replace"))
        if nodes:
            path.write_text(json.dumps(nodes, sort_keys=True))
    return json.loads(path.read_text())


def nearby_spotters(archive: Iterable[RbnSpot], nodes: dict[str, str], grid: str,
                    radius_mi: float) -> dict[str, float]:
    """Archive spotters within radius_mi of grid -> distance. A spotter missing from
    the node list (e.g. KM3T-5) falls back to its base call's location."""
    home = grid_latlon(grid)
    if home is None:
        raise ValueError(f"bad grid {grid!r}")
    out: dict[str, float] = {}
    for sp in {s.spotter for s in archive}:
        g = nodes.get(sp) or nodes.get(re.split(r"[-/]", sp)[0])
        ll = grid_latlon(g) if g else None
        if ll is not None:
            d = miles(home, ll)
            if d <= radius_mi:
                out[sp] = d
    return out


def nearby_recall(spots: dict[str, set[float]], archive: Iterable[RbnSpot],
                  near: set[str], start: datetime, end: datetime, bands: set[str],
                  tol_khz: float = DEFAULT_TOL_KHZ, min_nearby: int = DEFAULT_MIN_NEARBY
                  ) -> dict[str, tuple[set[str], bool]]:
    """Calls spotted on our bands in [start, end] by at least min_nearby nearby
    skimmers -> (those skimmers, whether we spotted the call within tol_khz of
    any of their frequencies)."""
    by_call: dict[str, tuple[set[str], set[float]]] = {}
    for s in archive:
        if s.spotter in near and start <= s.time <= end and band_of(s.freq_khz) in bands:
            sk, fs = by_call.setdefault(s.dx, (set(), set()))
            sk.add(s.spotter)
            fs.add(s.freq_khz)
    return {c: (sk, any(abs(f - g) <= tol_khz for f in fs for g in spots.get(c, ())))
            for c, (sk, fs) in by_call.items() if len(sk) >= min_nearby}


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
    ap.add_argument("--near", metavar="GRID",
                    help="also score against skimmers within --radius-mi of this grid")
    ap.add_argument("--radius-mi", type=float, default=DEFAULT_RADIUS_MI)
    ap.add_argument("--min-nearby", type=int, default=DEFAULT_MIN_NEARBY,
                    help="nearby recall: calls spotted by at least this many nearby skimmers")
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
    if a.near:
        near = nearby_spotters(archive, load_nodes(a.archive_dir), a.near, a.radius_mi)
        near = {sp: d for sp, d in near.items()
                if not any(sp.startswith(e.upper()) for e in a.exclude_spotter)}
        print(f"nearby skimmers (<= {a.radius_mi:.0f} mi of {a.near.upper()}): {len(near)}: "
              + " ".join(f"{sp}({d:.0f})" for sp, d in sorted(near.items(), key=lambda kv: kv[1])))
        hn = confirm(spots, archive, t0, t1, a.tol_khz, tuple(a.exclude_spotter), set(near))
        nc = sum(1 for sk in hn.values() if sk)
        print(f"nearby precision: {nc} of {n} spotted calls ({100.0 * nc / n if n else 0:.0f}%) "
              "heard by a nearby skimmer")
        bands = {b for fs in spots.values() for f in fs if (b := band_of(f))}
        rec = nearby_recall(spots, archive, set(near), a.start, a.end, bands,
                            a.tol_khz, a.min_nearby)
        got = sorted(c for c, (_sk, ok) in rec.items() if ok)
        missed = sorted(rec, key=lambda c: (-len(rec[c][0]), c))
        missed = [c for c in missed if not rec[c][1]]
        print(f"nearby recall ({', '.join(sorted(bands, key=lambda b: BANDS[b][0]))}; "
              f">= {a.min_nearby} nearby skimmer(s)): {len(got)} of {len(rec)} "
              f"({100.0 * len(got) / len(rec) if rec else 0:.0f}%)")
        print("missed: " + " ".join(f"{c}({len(rec[c][0])})" for c in missed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
