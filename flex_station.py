#!/usr/bin/env python3
"""Hold SparkGap's one GUI client (station) on a FlexRadio, so every band's copy binds to it.

Run it before the copies of a seat and stop it after the last one.  It reads the radio from a
SparkGap config (sdr_ip, sdr_port, flex_station), removes the pan and slice the radio gives a new
GUI client, pings the radio each second and registers again after a loss while its budget lasts.

    python3 flex_station.py --config config/seat.local.json --status logs/station.json

Exit codes: 0 stopped, 1 halted or failed to start, 3 the radio already has the station.
"""

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Any

from flex_iq import FlexStation, StationExists

log = logging.getLogger("sparkgap.flex")
EXIT_HALTED = 1
EXIT_EXISTS = 3
DEFAULT_CONTROL_PORT = 4992


_status_error: list[str] = []  # the last write error, logged once until a write succeeds


def write_status(path: Path, health: dict[str, Any]) -> None:
    """Write health as JSON, replacing the file in one step so a reader never sees half of it.
    A failed write is logged, never raised: status must not stop the station and its copies."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(health) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        if _status_error != [str(e)]:
            log.warning("[Flex] Station status not written to %s: %s", path, e)
            _status_error[:] = [str(e)]
        return
    if _status_error:
        log.info("[Flex] Station status written to %s again", path)
        _status_error.clear()


def station_from_config(path: Path) -> FlexStation:
    cfg = json.loads(path.read_text())
    if cfg.get("sdr_type") != "flex" or not cfg.get("sdr_ip"):
        raise SystemExit(f"{path}: not a Flex config with sdr_ip")
    return FlexStation(cfg["sdr_ip"], cfg.get("sdr_port") or DEFAULT_CONTROL_PORT,
                       cfg.get("flex_station"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    ap.add_argument("--config", type=Path, required=True, help="a SparkGap config for this radio")
    ap.add_argument("--status", type=Path, help="health JSON, rewritten on each heartbeat")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every control line")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    station = station_from_config(args.config)
    try:
        station.start()
    except StationExists as e:
        log.error("%s: leaving it to the station process that holds it", e)
        return EXIT_EXISTS
    except Exception as e:
        log.error("[Flex] Station did not start: %s", e)
        return EXIT_HALTED

    def on_signal(_sig: int, _frame: FrameType | None) -> None:
        station.request_stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    on_beat = (lambda h: write_status(args.status, h)) if args.status else None
    station.run(on_beat)
    station.stop()
    if args.status:
        write_status(args.status, station.health())
    return EXIT_HALTED if station.state == "halted" else 0


if __name__ == "__main__":
    sys.exit(main())
