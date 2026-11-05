#!/usr/bin/env python3
"""A radio's node settings for sparkgap.sh setup: spotter ID, grid and operator name, asked with
the current values as defaults, written to config/<radio>.radio.json and to the callsign and grid
of that radio's receiver configs.  Safe to run again: it changes only what the answers change.

Usage: setup_node.py <run root> <radio name>
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the code folder: radios.py
import radios  # noqa: E402


def ask(prompt: str, old: str | None, check: Callable[[str], str | None],
        read: Callable[[str], str]) -> str:
    """One answer: Enter keeps the old value; asked again until check passes (None)."""
    while True:
        got = read(f"{prompt}{f' [{old}]' if old else ''}: ").strip() or (old or "")
        why = check(got)
        if why is None:
            return got
        print(f"  {why}")


def is_running(root: Path, name: str) -> bool:
    """Whether a receiver runs: its pid file names a live process."""
    try:
        os.kill(int((root / "logs" / f"{name}.pid").read_text().strip()), 0)
    except (OSError, ValueError):
        return False
    return True


def setup(root: Path, radio: str, read: Callable[[str], str] = input) -> list[str]:
    """Ask the node settings, show what would change and what restarts, and write only on yes.
    What changed, one line each; a last line "restart: <receivers>" or "restart-feeder: <receiver>"
    tells sparkgap.sh what to restart."""
    st: dict[str, Any] = json.loads((root / "config" / f"{radio}.radio.json").read_text())
    print(f"{radio}: {st.get('model', 'radio')} at {st.get('address', '?')}")
    call = ask("Spotter ID (the callsign RBN shows)", st.get("callsign"), radios.check_call, read)
    grid = ask("Grid", st.get("grid"), radios.check_grid, read)
    name = ask("Operator name", st.get("op_name"), radios.check_name, read)
    config = root / "config"
    lines, receivers = radios.set_node(config, radio, call, grid, name, write=False)
    if lines == [f"{radio}: unchanged"]:
        return lines
    mine = [c.name.removesuffix(".local.json") for c in sorted(config.glob("*.local.json"))
            if json.loads(c.read_text()).get("radio") == radio]
    running = [r for r in receivers if is_running(root, r)]
    feeder = [r for r in mine if is_running(root, r)][:1] if not running else []
    then = (f" and restart {', '.join(running)}" if running
            else " and restart the feeder" if feeder else "")
    print("\n".join(lines))
    if read(f"Save{then}? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        return ["nothing changed"]
    radios.set_node(config, radio, call, grid, name)
    return lines + ([f"restart: {' '.join(running)}"] if running else
                    [f"restart-feeder: {feeder[0]}"] if feeder else [])


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    root, radio = Path(sys.argv[1]), sys.argv[2]
    try:
        lines = setup(root, radio)
    except (OSError, ValueError) as e:
        print(f"setup: {e}", file=sys.stderr)
        return 2
    except EOFError:
        print("\nsetup: no answer, nothing changed", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
