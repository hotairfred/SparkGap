#!/usr/bin/env python3
"""Hold SparkGap's one GUI client (station) on a FlexRadio, so every band's copy binds to it.

Run it before the copies of a seat and stop it after the last one.  It reads the radio from a
SparkGap config (sdr_ip, sdr_port, flex_station), removes the pan and slice the radio gives a new
GUI client, pings the radio each second and registers again after a loss while its budget lasts.

    python3 flex_station.py --config config/seat.local.json

Its health goes to the SparkGap server over its link (process_link.py) each beat.

Exit codes: 0 stopped, 1 halted or failed to start, 3 the radio already has the station.
"""

import argparse
import json
import logging
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Any

from flex_iq import FlexStation, StationExists, bind_radio, radio_path
from process_link import ProcessLink

log = logging.getLogger("sparkgap.flex")
EXIT_HALTED = 1
EXIT_EXISTS = 3
DEFAULT_CONTROL_PORT = 4992


def station_from_config(path: Path) -> FlexStation:
    cfg = json.loads(path.read_text())
    if cfg.get("sdr_type") != "flex" or not (cfg.get("radio") or cfg.get("sdr_ip")):
        raise SystemExit(f"{path}: not a Flex config with radio or sdr_ip")
    # radio: the radio file (Discover) gives its address and, if it moves, finds it again
    ip = bind_radio(radio_path(path.parent, cfg["radio"])) if cfg.get("radio") else cfg["sdr_ip"]
    return FlexStation(ip, cfg.get("sdr_port") or DEFAULT_CONTROL_PORT,
                       cfg.get("flex_station"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    ap.add_argument("--config", type=Path, required=True, help="a SparkGap config for this radio")
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

    def on_command(cmd: str, _args: dict[str, Any]) -> tuple[bool, str]:
        if cmd == "stop":
            log.info("[Flex] Station stop asked by the server")
            station.request_stop()
            return True, "station stopping"
        return False, f"unknown command {cmd!r}"

    # the server's view of this station (cdub89/SparkGap#33): its health each beat, and stop
    cfg = json.loads(args.config.read_text())
    link = ProcessLink("station", str(cfg.get("radio") or cfg.get("sdr_ip")), on_command).start()

    station.run(lambda h: link.status(**h))
    station.stop()
    link.status(**station.health())
    link.close()
    return EXIT_HALTED if station.state == "halted" else 0


if __name__ == "__main__":
    sys.exit(main())
