#!/usr/bin/env python3
"""Refit the spot validation weights (spot_rule.py _WEIGHTS_LISTED) from run logs.

Each log is replayed through the spot rule with its config, and every validation check is
recorded: the score's inputs (one per weight) and whether RBN confirmed that call there. A check
is confirmed when another RBN skimmer spotted the same call within 1 kHz and 10 minutes (the
public daily archive, fetched to --archive-dir). Checks of one call in one kHz, less than 10
minutes apart, form an episode; the fit is a logistic regression with each episode weighted
once. It prints the weights and, per level, the threshold that emits about as many episodes as
today's weights at today's threshold. Nothing is written to spot_rule.py.

--holdout fits on all logs but one and compares on that one, at the unconfirmed count today's
weights give there (episode level, before the later gates; replay a log end to end to confirm).

Needs logs with the "ITILA raw" lines, a config with spot_validation on and spot_validation_calls
rbn or rbn+skcc. Log times are local: the UTC offset comes from the log's "RBN:" lines, or give
--start (the UTC time of the log's first line). The log has no SNR per window, so a config
that picks between copies by SNR (spot_rule_strongest_db) replays a little differently from
live. Run from the repo root.

  python3 tools/eval/fit_validation.py --config X.local.json --callsign N0CALL-1 A.log B.log C.log
"""

from __future__ import annotations

import argparse
import ast
import bisect
import json
import logging
import re
import sys
import urllib.error
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools" / "eval"))

from rbn_confirm import RbnSpot, load_day  # noqa: E402  # pyright: ignore[reportMissingImports]

import rbn_known  # noqa: E402
import spot_rule  # noqa: E402

UTC = timezone.utc
EPISODE_GAP_S = 600.0        # checks of one call further apart start a new episode
CONFIRM_KHZ = 1.0            # another skimmer's spot this close confirms the call
CONFIRM_S = 600.0            # ... and this close in time
L2 = 0.01                    # ridge on every weight but bias
DAY_S = 86_400

_TIME = re.compile(r"^(\d\d):(\d\d):(\d\d) ")
_RAW = re.compile(r"^(\d\d):(\d\d):(\d\d) INFO ITILA raw ([\d.]+) kHz cost=([\d.]+): (.*)$")
_RBN = re.compile(r"^(\d\d):(\d\d):(\d\d) INFO RBN: [^,]*,,,[\d.]+,\w*,[A-Z0-9/]+,,,\w+,[^,]*,"
                  r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),")


@dataclass(frozen=True)
class Window:
    t: float                 # UTC seconds
    freq_khz: float
    cost: float
    text: str


@dataclass(frozen=True)
class Check:
    t: float
    freq_khz: float
    call: str
    feats: dict[str, float]


@dataclass
class _Episode:
    t: float
    rows: list[list[float]] = field(default_factory=list)
    confirmed: bool = False


def _secs(m: re.Match[str]) -> int:
    return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])


def parse_log(lines: Iterable[str], start: datetime | None = None) -> list[Window]:
    """The decode windows of a log, in UTC. Local times roll over at midnight; the offset is
    start (UTC of the first line) or the first "RBN:" line's own UTC time."""
    rows: list[tuple[int, float, float, str]] = []    # (local s from day one, freq, cost, text)
    day, last, first, offset = 0, None, None, None
    for line in lines:
        mt = _TIME.match(line)
        if not mt:
            continue
        lt = _secs(mt)
        if last is not None and lt < last - DAY_S // 2:
            day += 1
        last = lt
        local = day * DAY_S + lt
        if first is None:
            first = local
        raw = "ITILA raw" in line
        if not raw and "INFO RBN: " not in line:
            continue
        m = (_RAW if raw else _RBN).match(line)
        if not m:
            continue
        if raw:
            try:
                text = ast.literal_eval(m[6])
            except (ValueError, SyntaxError):
                text = m[6].strip()[1:-1]
            rows.append((local, float(m[4]), float(m[5]), str(text)))
        elif offset is None and start is None:
            utc = datetime.fromisoformat(m[4]).replace(tzinfo=UTC).timestamp()
            offset = round((utc - local) / 900) * 900          # whole quarter hours
    if start is not None and first is not None:
        offset = start.timestamp() - first
    if offset is None:
        if rows:
            raise ValueError("no RBN: line to place the log in UTC; give --start")
        return []
    return [Window(offset + lt, f, c, x) for lt, f, c, x in rows]


def start_day(windows: list[Window]) -> date:
    """The UTC day the log starts: the known-call list is the one in force then."""
    return datetime.fromtimestamp(windows[0].t, UTC).date()


def make_rule(config: dict, own_call: str, cache_dir: Path, today: date):
    """The spot rule of config, with its call list as it stood on today."""
    import sparkgap as sg
    calls = config.get("spot_validation_calls")
    if calls not in ("rbn", "rbn+skcc"):
        raise SystemExit("spot_validation_calls must be rbn or rbn+skcc")
    live = config.get("rbn_known_dir")
    if live and Path(live).resolve() == cache_dir.resolve():
        raise SystemExit("--cache-dir is the live rbn_known_dir; give the tool its own")
    days = int(config.get("rbn_known_days", rbn_known.DAYS))
    known = rbn_known.refresh(cache_dir, own_call, days, today=today,
                              drop_busts=bool(config.get("rbn_known_drop_busts", False)),
                              keep=sg._scp_lookup(config))
    if calls == "rbn+skcc":
        import skcc_roster
        known = known | skcc_roster.refresh(cache_dir)
    tracker = sg.SpotTracker(set(), set())
    tracker.__dict__["_validation_lists"] = {calls: lambda c: rbn_known.base_call(c) in known}
    rule = sg._make_spot_rule(config, tracker)
    if getattr(rule, "threshold", None) is None:
        raise SystemExit("spot_validation is off in this config: no checks to fit")
    return rule


def record_checks(rule, windows: list[Window], max_cost: float | None) -> list[Check]:
    """Replay windows through rule (as live: same window ids, the cost gate) and record
    each validation check's inputs. score() is linear in rule.weights, so each input is the score
    with that weight 1 and the rest 0: nothing here copies how score() reads a call."""
    keys = list(rule.weights)
    real = rule.weights
    orig = rule.score
    checks: list[Check] = []
    now: list[float] = [0.0, 0.0]

    def score(call, near, counts, heard_before):
        s = orig(call, near, counts, heard_before)
        feats = {}
        try:
            for k in keys:
                rule.weights = {x: float(x == k) for x in keys}
                feats[k] = float(orig(call, near, counts, heard_before))
        finally:
            rule.weights = real
        if abs(sum(real[k] * v for k, v in feats.items()) - s) > 1e-6:
            raise RuntimeError("score() is no longer linear in its weights; update this tool")
        checks.append(Check(now[0], now[1], call, feats))
        return s

    rule.score = score
    try:
        wkey, prev = 0, None
        for w in windows:
            if max_cost is not None and w.cost > max_cost:
                prev = None
                continue
            if not (prev is not None and prev[1] == w.freq_khz and abs(prev[0] - w.t) <= 1.0):
                wkey += 1
            prev = (w.t, w.freq_khz)
            now[0], now[1] = w.t, w.freq_khz
            wid = rule.window_id(int(round(w.freq_khz * 10)), wkey, w.t)
            for _ in rule.feed(wid, w.freq_khz, w.text, w.t, None, None):
                pass
    finally:
        rule.score = orig
    return checks


class Confirmer:
    """Other skimmers' archive spots, by kHz, for confirmed()."""

    def __init__(self, spots: Iterable[RbnSpot], own_call: str):
        own = rbn_known.spotter(own_call) if own_call else None
        by: defaultdict[int, list[tuple[float, float, str]]] = defaultdict(list)
        for s in spots:
            if own and rbn_known.spotter(s.spotter) == own:
                continue
            by[int(s.freq_khz)].append((s.time.timestamp(), s.freq_khz, rbn_known.base_call(s.dx)))
        for v in by.values():
            v.sort()
        self._by = dict(by)

    def confirmed(self, t: float, freq_khz: float, call: str) -> bool:
        base = rbn_known.base_call(call)
        for k in (int(freq_khz) - 1, int(freq_khz), int(freq_khz) + 1):
            lst = self._by.get(k, [])
            i = bisect.bisect_left(lst, (t - CONFIRM_S,))
            while i < len(lst) and lst[i][0] <= t + CONFIRM_S:
                if lst[i][2] == base and abs(lst[i][1] - freq_khz) <= CONFIRM_KHZ:
                    return True
                i += 1
        return False


Episodes = list[tuple[np.ndarray, bool]]


def episodes(checks: Iterable[Check], confirmed: Iterable[bool], keys: list[str]) -> Episodes:
    """Group checks into episodes: one call in one kHz, gaps of EPISODE_GAP_S at most.
    An episode is confirmed when any of its checks is."""
    out: list[_Episode] = []
    open_: dict[tuple[str, int], _Episode] = {}
    for c, ok in zip(checks, confirmed, strict=True):
        key = (c.call, int(c.freq_khz))
        ep = open_.get(key)
        if ep is None or c.t - ep.t > EPISODE_GAP_S:
            ep = _Episode(c.t)
            out.append(ep)
            open_[key] = ep
        ep.t = c.t
        ep.rows.append([c.feats[k] for k in keys])
        ep.confirmed |= ok
    return [(np.array(e.rows), e.confirmed) for e in out]


def fit(eps: Episodes, keys: list[str], use: list[str]) -> dict[str, float]:
    """Logistic fit of use (bias first), each episode weighted once, ridge L2 on all but bias."""
    idx = [keys.index(k) for k in use]
    x = np.vstack([e[0][:, idx] for e in eps])
    y = np.concatenate([[float(e[1])] * len(e[0]) for e in eps])
    wt = np.concatenate([[1.0 / len(e[0])] * len(e[0]) for e in eps])

    def loss(b: np.ndarray) -> float:
        z = x @ b
        return float(np.sum(wt * (np.logaddexp(0, z) - y * z)) + L2 * np.sum(b[1:] ** 2))

    def grad(b: np.ndarray) -> np.ndarray:
        p = 1 / (1 + np.exp(-(x @ b)))
        g = x.T @ (wt * (p - y))
        g[1:] += 2 * L2 * b[1:]
        return g

    b = minimize(loss, np.zeros(len(idx)), jac=grad, method="L-BFGS-B").x
    return {k: float(v) for k, v in zip(use, b, strict=True)}


def _best(eps: Episodes, keys: list[str], w: dict[str, float]) -> np.ndarray:
    v = np.array([w.get(k, 0.0) for k in keys])
    return np.array([float((x @ v).max()) for x, _ in eps])


def matched_threshold(eps: Episodes, keys: list[str], w: dict[str, float],
                      ref: dict[str, float], ref_thr: float) -> float:
    """The threshold at which w emits about as many episodes as ref at ref_thr (ties; no labels)."""
    n = int((_best(eps, keys, ref) >= ref_thr).sum())
    if n == 0:
        return float("inf")
    m = np.sort(_best(eps, keys, w))[::-1]
    return float(m[n - 1])


def decide(eps: Episodes, keys: list[str], w: dict[str, float], thr: float) -> tuple[int, int]:
    """(confirmed, unconfirmed) episodes emitted at thr."""
    s = _best(eps, keys, w)
    conf = np.array([c for _, c in eps], dtype=bool)
    return int((conf & (s >= thr)).sum()), int((~conf & (s >= thr)).sum())


def at_unconfirmed(eps: Episodes, keys: list[str], w: dict[str, float],
                   target: int) -> tuple[int, int]:
    """(confirmed, unconfirmed) at the lowest threshold with no more than target unconfirmed."""
    s = _best(eps, keys, w)
    conf = [c for _, c in eps]
    bad: Counter[float] = Counter()
    for v, c in zip(s, conf, strict=True):
        bad[round(float(v), 9)] += not c
    thr, u = max(bad) + 1e-6, 0
    for v in sorted(bad, reverse=True):          # a tied score is taken whole or not at all
        u += bad[v]
        if u > target:
            break
        thr = v
    return decide(eps, keys, w, thr)


def _days_of(windows: list[Window]) -> Iterator[str]:
    d = datetime.fromtimestamp(windows[0].t, UTC).date()
    last = datetime.fromtimestamp(windows[-1].t + CONFIRM_S, UTC).date()
    while d <= last:
        yield f"{d:%Y%m%d}"
        d += timedelta(days=1)


def _archive_day(day: str, archive_dir: Path) -> list[RbnSpot]:
    """One day of the archive; RBN posts it the day after, and a log is not scored without it."""
    try:
        return load_day(day, archive_dir)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SystemExit(f"no RBN archive for {day} yet; try again once it is posted") from e
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+", type=Path)
    ap.add_argument("--config", required=True, type=Path, help="the config the logs ran with")
    ap.add_argument("--callsign", help="the node's spotter ID (default: config callsign);"
                    " its own spots never confirm")
    ap.add_argument("--start", action="append", default=[],
                    help="UTC time of a log's first line, 'YYYY-MM-DD HH:MM:SS', one per log")
    ap.add_argument("--cache-dir", type=Path, default=Path("rbn_data") / "known",
                    help="known-call days and SKCC roster (not the live rbn_known_dir:"
                    " the list is rebuilt as of each log)")
    ap.add_argument("--archive-dir", type=Path, default=Path("rbn_data"), help="RBN daily archives")
    ap.add_argument("--features", help="comma-separated weights to fit (default: all); rest are 0")
    ap.add_argument("--holdout", action="store_true",
                    help="also test each log on a fit of the others")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    config = json.loads(a.config.read_text())
    config.pop("spot_rule_memory", None)            # start clean, as each log did
    gate = config.get("gate_timing_cost", False)
    max_cost = float(config.get("timing_cost_max", 30.0)) if gate else None
    own = a.callsign or config.get("callsign", "")
    if a.start and len(a.start) != len(a.logs):
        raise SystemExit("--start: one per log")
    per_log = []
    keys: list[str] = []
    ref: dict[str, float] = {}
    for i, path in enumerate(a.logs):
        start = datetime.fromisoformat(a.start[i]).replace(tzinfo=UTC) if a.start else None
        with path.open(errors="replace") as fh:
            windows = parse_log(fh, start)
        if not windows:
            raise SystemExit(f"{path}: no ITILA raw lines")
        rule = make_rule(config, own, a.cache_dir, start_day(windows))
        keys, ref = list(rule.weights), dict(rule.weights)
        checks = record_checks(rule, windows, max_cost)
        archive = [s for d in _days_of(windows) for s in _archive_day(d, a.archive_dir)]
        judge = Confirmer(archive, own)
        eps = episodes(checks, (judge.confirmed(c.t, c.freq_khz, c.call) for c in checks), keys)
        per_log.append((path.name, eps))
        print(f"{path.name}: {len(windows)} windows, {len(checks)} checks, {len(eps)} episodes, "
              f"{sum(c for _, c in eps)} confirmed", file=sys.stderr)

    use = ["bias"] + [k for k in (a.features.split(",") if a.features else keys) if k != "bias"]
    bad = [k for k in use if k not in keys]
    if bad:
        raise SystemExit(f"unknown features: {bad}; known: {keys}")
    every = [e for _, eps in per_log for e in eps]
    w = fit(every, keys, use)
    print("weights (all logs):")
    print("  {" + ", ".join(f'"{k}": {w.get(k, 0.0):.2f}' for k in keys) + "}")
    print("thresholds, emitting about as many episodes as today's weights at today's level:")
    for lvl, t0 in spot_rule.VALIDATION.items():
        print(f"  {lvl}: {matched_threshold(every, keys, w, ref, t0):.2f}   (today {t0})")
    if a.holdout and len(per_log) > 1:
        normal = spot_rule.VALIDATION["normal"]
        print("holdout (confirmed episodes at today's unconfirmed count, normal):")
        for name, test in per_log:
            train = [e for n, eps in per_log if n != name for e in eps]
            c0, u0 = decide(test, keys, ref, normal)
            c1, u1 = at_unconfirmed(test, keys, fit(train, keys, use), u0)
            print(f"  {name}: today {c0} confirmed / {u0} unconfirmed; refit {c1} / {u1}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
