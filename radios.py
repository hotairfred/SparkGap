"""The SDR back ends behind one interface (docs/RADIOS.md, cdub89/SparkGap#33).

The control service, SparkGap Web and the command line see a radio, its receivers and its feeder;
what each kind of radio needs to get there (the Flex station, DAX-IQ channels, serial lookups,
HPSDR receiver indexes) stays in its back end.  A back end says what radios it hears (discover)
and what its receivers can change (capabilities).
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DISCOVER_S = 3.0  # a FlexRadio broadcasts about once a second
# DAX-IQ channels by model (FlexLib ModelInfo.MaxDaxIqChannels); others have 4
DAXIQ_CHANNELS = {"FLEX-6300": 2, "FLEX-6400": 2, "FLEX-6400M": 2, "FLEX-8400": 2, "FLEX-8400M": 2}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")  # a radio name usable as a file name


@dataclass(frozen=True)
class Capabilities:
    """What a receiver of this kind of radio can change, and what runs besides its skimmers."""

    rx_settings: tuple[str, ...] = ()  # receive path settings, changed live
    live_band: bool = False  # a running receiver moves band in place
    station: bool = False  # the radio needs a station (one GUI client its receivers share)


@dataclass(frozen=True)
class Found:
    """A radio heard on the network."""

    type: str
    name: str
    model: str
    address: str
    identity: dict[str, str] = field(default_factory=dict)  # how it is found again (serial)
    facts: dict[str, Any] = field(default_factory=dict)  # what it holds (num_scu, max_slices, ...)


class Backend(Protocol):
    type: str
    capabilities: Capabilities

    def discover(self, seconds: float, address: str | None = None) -> list[Found]: ...


class Flex:
    """FlexRadio: SmartSDR's discovery broadcast on UDP 4992; a pan and DAX-IQ channel per
    receiver, one skimmer each, the station shared."""

    type = "flex"
    capabilities = Capabilities(rx_settings=("antenna", "loop_a", "loop_b", "rf_gain"),
                                live_band=True, station=True)

    def __init__(self, heard: Callable[[float], dict[str, dict[str, str]]] | None = None,
                 ask: Callable[[str], dict[str, str]] | None = None) -> None:
        self.heard = heard
        self.ask = ask

    def discover(self, seconds: float, address: str | None = None) -> list[Found]:
        """The radios broadcasting on this network, or the one at `address` (where the broadcast
        does not arrive: WSL, or SmartSDR holding the port on Windows); each asked "info"."""
        import flex_iq  # the copy's own module: the service runs from a code folder

        ask = self.ask or flex_iq.ask_info
        info: dict[str, str] = {}
        if address:
            try:
                info = ask(address)
            except OSError as e:
                raise RuntimeError(f"no FlexRadio answers at {address}: {e}") from None
            if not info.get("chassis_serial"):
                raise RuntimeError(f"{address} did not answer like a FlexRadio")
            heard = {info["chassis_serial"]: {"ip": address}}
        else:
            heard = (self.heard or flex_iq.discover)(seconds)
        out = []
        for serial, kv in heard.items():
            if not address:
                try:
                    info = ask(kv["ip"])
                except OSError:
                    info = {}  # what the broadcast says is enough to find it again
            model = info.get("model") or kv.get("model", "")
            name = info.get("name") or kv.get("nickname") or f"{model or 'Flex'}-{serial[-4:]}"
            out.append(Found(self.type, name, model, kv["ip"], {"serial": serial},
                             flex_facts(model, kv, info)))
        return sorted(out, key=lambda r: r.name)


def number(v: str | None) -> int | None:
    return int(v) if v and v.isdigit() else None


def flex_facts(model: str, kv: dict[str, str], info: dict[str, str]) -> dict[str, Any]:
    """What a FlexRadio holds, from its broadcast and "info": SCUs, slices, pans, DAX-IQ."""
    slices = number(kv.get("max_slices")) or number(info.get("num_slice"))
    facts = {"num_scu": number(info.get("num_scu")), "max_slices": slices,
             "max_panadapters": number(kv.get("max_panadapters")) or slices,  # one pan per slice
             "daxiq_channels": DAXIQ_CHANNELS.get(model, 4) if model else None,
             "software": info.get("software_ver") or kv.get("version")}
    return {k: v for k, v in facts.items() if v is not None}


def receiver_limit(radio: dict[str, Any]) -> int | None:
    """Most receivers a radio file's radio holds (a pan, a slice and a DAX-IQ channel each);
    None when unknown."""
    caps = [radio.get(k) for k in ("max_panadapters", "max_slices", "daxiq_channels")]
    known = [c for c in caps if isinstance(c, int) and c > 0]
    return min(known) if known else None


BACKENDS: dict[str, Backend] = {"flex": Flex()}


def backend(sdr_type: Any) -> Backend | None:
    """The back end of a receiver's sdr_type, None for a radio SparkGap runs without one."""
    return BACKENDS.get(str(sdr_type))


def capabilities(sdr_type: Any) -> Capabilities:
    b = backend(sdr_type)
    return b.capabilities if b else Capabilities()


def write_radio(path: Path, st: dict[str, Any]) -> None:
    """A radio file in one atomic replace."""
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(json.dumps(st, indent=2) + "\n")
    os.replace(tmp, path)


def record(config_dir: Path, r: Found) -> str:
    """Write what Discover found to config/<name>.radio.json, keeping the operator's node settings;
    one line saying what changed."""
    if not NAME_RE.match(r.name):
        return f"{r.name}: a name this cannot use as a file name, skipped"
    path = config_dir / f"{r.name}.radio.json"
    for other in sorted(config_dir.glob("*.radio.json")):  # the same radio under its old name
        if other == path or not r.identity:
            continue
        try:
            st0: Any = json.loads(other.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(st0, dict) and all(st0.get(k) == v for k, v in r.identity.items()):
            return (f"{r.name}: the radio {other.name} holds (renamed on the radio), skipped: "
                    f"set its name back, or rename that file and its receivers")
    try:
        old: Any = json.loads(path.read_text())
    except (OSError, ValueError):
        old = None
    old = old if isinstance(old, dict) else None
    found = {"type": r.type, "nickname": r.name, "model": r.model, **r.identity,
             "address": r.address, **r.facts}
    st = {**(old or {}), **found}
    if old == st:
        return f"{r.name}: {r.model} at {r.address}, unchanged"
    write_radio(path, st)
    was = old.get("address") if old else None
    moved = f", was {was}" if old and was != r.address else ""
    return f"{r.name}: {r.model} at {r.address}{moved}{'' if old else ', new'}"


def discover(config_dir: Path, seconds: float = DISCOVER_S, address: str | None = None) -> str:
    """Every back end's radios on the local network, or the radio at `address`, each written to its
    radio file."""
    if address:
        heard = BACKENDS["flex"].discover(seconds, address)  # the one back end asked by address
    else:
        heard = [r for b in BACKENDS.values() for r in b.discover(seconds)]
    if not heard:
        return (f"no radio heard in {seconds:.0f} s: give its address "
                "(Setup in SparkGap Web, or sparkgap.sh setup)")
    return "; ".join(record(config_dir, r) for r in heard)


# ---- a radio's node settings (sparkgap.sh setup and the server's "node" command) ----

CALL_RE = re.compile(r"^(?=[A-Z0-9/]*[0-9])[A-Z0-9]+(/[A-Z0-9]+)?(-[0-9]{1,2})?$")  # WX7V-2
GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$")  # EM12 or EM12ub
UPLOAD_DEFAULT = "dry"  # a new node never posts to RBN; sparkgap.sh rbn turns it live


def check_call(v: str) -> str | None:
    if CALL_RE.match(v.upper()):
        return None
    return "a callsign, with its SSID if the node has one (N0CALL-2)"


def check_grid(v: str) -> str | None:
    return None if GRID_RE.match(v.upper()) else "a 4 or 6 character grid (EM12 or EM12ub)"


def check_name(v: str) -> str | None:
    return None if v.strip() else "the operator's name, as RBN shows it"


def grid_case(g: str) -> str:
    return g[:4].upper() + g[4:].lower()


def write_json_kept(path: Path, data: dict[str, Any], backup: str) -> None:
    """Write data over path, keeping the old file as <path>.<backup>."""
    if path.exists():
        path.with_name(f"{path.name}.{backup}").write_bytes(path.read_bytes())
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def set_node(config_dir: Path, radio: str, callsign: str, grid: str,
             op_name: str, write: bool = True) -> tuple[list[str], list[str]]:
    """A radio's spotter ID, grid and operator: checked and written to its radio file (RBN upload
    dry when unset), the old file kept; its receivers read them from there.  Returns what changed,
    one line each, and the receivers that take a new spotter ID or grid at their next start
    (write=False: what would change, nothing written).  ValueError: nothing is written."""
    if not NAME_RE.match(radio):
        raise ValueError(f"{radio}: not a radio name")
    path = config_dir / f"{radio}.radio.json"
    try:
        st: dict[str, Any] = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ValueError(f"no radio file for {radio}: find the radio first") from None
    for why in (check_call(callsign), check_grid(grid), check_name(op_name)):
        if why:
            raise ValueError(why)
    new = {**st, "callsign": callsign.upper(), "grid": grid_case(grid), "op_name": op_name.strip()}
    new.setdefault("rbn_upload", UPLOAD_DEFAULT)
    backup = f"pre-setup-{time.strftime('%Y%m%d_%H%M%SZ', time.gmtime())}"
    done, receivers = [], []
    if new != st:
        if write:
            write_json_kept(path, new, backup)
        done.append(f"{path.name}: {new['callsign']} {new['grid']} {new['op_name']}, "
                    f"rbn upload {new['rbn_upload']}")
    moved = (new["callsign"], new["grid"]) != (st.get("callsign"), st.get("grid"))
    for conf in sorted(config_dir.glob("*.local.json")):
        c = json.loads(conf.read_text())
        if c.get("radio") != radio:
            continue
        # its receivers read spotter ID and grid from the radio file: an older copy here goes
        want = {k: v for k, v in c.items() if k not in ("callsign", "grid", "skimmer_suffix")}
        if want != c:
            if write:
                write_json_kept(conf, want, backup)
            done.append(f"{conf.name}: spotter ID and grid from {path.name}")
        if moved:
            receivers.append(conf.name.removesuffix(".local.json"))
    return done or [f"{radio}: unchanged"], receivers


def radio_files(config_dir: Path) -> list[dict[str, Any]]:
    """Every radio file, as setup shows it: name, model, address, node settings, what it holds."""
    out = []
    for path in sorted(config_dir.glob("*.radio.json")):
        try:
            st = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(st, dict):
            out.append({"name": path.name.removesuffix(".radio.json"), **{k: st.get(k) for k in (
                "type", "model", "address", "callsign", "grid", "op_name", "rbn_upload",
                "max_panadapters", "max_slices", "daxiq_channels")}})
    return out
