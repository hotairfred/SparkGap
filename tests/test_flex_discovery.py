"""Discover writes each radio's radio file (config/<nickname>.radio.json); a seat names its
radio with radio, and a radio that moves (DHCP after a power cut, Dallas 2026-11-02) is found
again by the serial in that file, its new address written back."""

import json
from pathlib import Path

import pytest

import flex_iq
from seat import Seat

PACKET = (b"\x38\x52\x00\x00" + b"\x00" * 24
          + b'discovery_protocol_version=3.0.0.3 model=FLEX-6400M '
          b'serial=0520-0945-6401-3114 version=4.3.16.42034 nickname=Dallas callsign=WX7V '
          b'ip=192.168.1.150 port=4992 status=Available\x00\x00')
DALLAS = {"serial": "0520-0945-6401-3114", "ip": "192.168.1.150", "nickname": "Dallas",
          "model": "FLEX-6400M"}
LAKE = {"serial": "3718-0522-6600-0003", "ip": "192.168.0.101", "nickname": "Lake",
        "model": "FLEX-6600"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flex_iq, "_radio_serial", None)
    monkeypatch.setattr(flex_iq, "on_radio_moved", None)


def test_a_discovery_packet_gives_serial_and_address() -> None:
    kv = flex_iq.parse_discovery(PACKET)
    assert kv is not None
    got = (kv["serial"], kv["ip"], kv["nickname"])
    assert got == ("0520-0945-6401-3114", "192.168.1.150", "Dallas")
    assert flex_iq.parse_discovery(b"not a radio") is None


def test_a_moved_radio_is_found_by_its_serial() -> None:
    flex_iq.set_radio_serial("0520-0945-6401-3114")
    radios = {r["serial"]: r for r in (DALLAS, LAKE)}
    assert flex_iq.locate("192.168.1.136", radios) == "192.168.1.150"


def test_without_a_serial_nothing_is_guessed() -> None:
    assert flex_iq.locate("192.168.1.136", {DALLAS["serial"]: DALLAS}) is None


def test_the_station_config_follows_a_move(tmp_path: Path) -> None:
    path = flex_iq.radio_path(tmp_path, "Dallas")
    flex_iq.write_radio(path, dict(DALLAS, address="192.168.1.136"))
    assert flex_iq.bind_radio(path) == "192.168.1.136"
    assert flex_iq._radio_serial == "0520-0945-6401-3114"
    assert flex_iq.on_radio_moved is not None
    flex_iq.on_radio_moved("192.168.1.150")
    assert json.loads(path.read_text())["address"] == "192.168.1.150"


def test_no_station_config_says_run_discover(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="Discover"):
        flex_iq.bind_radio(flex_iq.radio_path(tmp_path, "Dallas"))


def test_discover_writes_each_radios_station_config(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "instances").write_text("# none\n")
    both = {r["serial"]: r for r in (DALLAS, LAKE)}
    monkeypatch.setattr(flex_iq, "discover", lambda seconds=0: both)
    seat = Seat(tmp_path)
    first = seat.discover()
    assert "Dallas: FLEX-6400M at 192.168.1.150, new" in first and "Lake: FLEX-6600" in first
    st = json.loads((tmp_path / "config" / "Dallas.radio.json").read_text())
    assert st == {"type": "flex", "nickname": "Dallas", "model": "FLEX-6400M",
                  "serial": "0520-0945-6401-3114", "address": "192.168.1.150", "daxiq_channels": 2}
    monkeypatch.setattr(flex_iq, "discover",
                        lambda seconds=0: {DALLAS["serial"]: dict(DALLAS, ip="192.168.1.151")})
    assert "Dallas: FLEX-6400M at 192.168.1.151, was 192.168.1.150" in seat.discover()


def test_discover_keeps_the_node_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A radio found at a new address keeps its spotter ID, grid and rbn_upload."""
    (tmp_path / "config").mkdir()
    (tmp_path / "instances").write_text("# none\n")
    node = {"callsign": "N0CALL-5", "grid": "EM12ou", "rbn_upload": "dry"}
    path = tmp_path / "config" / "Dallas.radio.json"
    st = {"type": "flex", "nickname": "Dallas", "address": "192.0.2.1", **node}
    path.write_text(json.dumps(st))
    monkeypatch.setattr(flex_iq, "discover", lambda seconds=0: {DALLAS["serial"]: DALLAS})
    assert "was 192.0.2.1" in Seat(tmp_path).discover()
    st = json.loads(path.read_text())
    assert st["address"] == "192.168.1.150" and {k: st[k] for k in node} == node
