"""The HPSDR back end (docs/WEB888_DESIGN.md): Web-888 discovery, a radio known by its address,
and discovery by radio type."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import radios

# what our Web-888 answered on 2026-11-06 (hpsdr_receiver.discover's fields)
WEB888 = {"ip": "192.0.2.120", "port": 1024, "mac": "64:69:73:74:72:6f", "status": 2,
          "board_id": 6, "gateware": 73, "receivers": 6}


def _heard(*devices: dict[str, Any]) -> radios.Hpsdr:
    return radios.Hpsdr(lambda _s, _a: list(devices))


def test_a_web888_is_named_and_known_by_its_address() -> None:
    (r,) = _heard(WEB888).discover(1.0)
    assert (r.type, r.name, r.model, r.address, r.identity, r.note) == (
        "hpsdr", "Web888", "Web-888", "192.0.2.120", {"address": "192.0.2.120"}, "")
    assert r.facts == {"board_id": 6, "firmware": 73, "receivers": 6}


def test_another_board_is_named_by_its_board_id() -> None:
    (r,) = _heard({**WEB888, "board_id": 1, "gateware": 25, "receivers": 8}).discover(1.0)
    assert (r.name, r.model, r.facts["receivers"]) == ("HPSDR", "HPSDR board 1", 8)


def test_a_radio_held_by_another_client_says_so() -> None:
    (r,) = _heard({**WEB888, "status": radios.HPSDR_BUSY}).discover(1.0)
    assert r.note == "in use by another client"


def test_by_address_the_radio_is_asked_directly() -> None:
    asked: list[str] = []
    radios.Hpsdr(lambda _s, a: asked.append(a) or [WEB888]).discover(1.0, "192.0.2.120")
    radios.Hpsdr(lambda _s, a: asked.append(a) or [WEB888]).discover(1.0)
    assert asked == ["192.0.2.120", radios.HPSDR_BROADCAST]
    with pytest.raises(RuntimeError, match="no HPSDR radio answers at 192.0.2.9"):
        _heard().discover(1.0, "192.0.2.9")


def test_a_second_unit_never_takes_the_first_one_s_file(tmp_path: Path) -> None:
    first = radios.Found("hpsdr", "Web888", "Web-888", "192.0.2.120", {"address": "192.0.2.120"})
    assert radios.free_name(tmp_path, first) == first  # no file yet
    radios.record(tmp_path, first)
    assert radios.free_name(tmp_path, first) == first  # its own file
    second = radios.Found("hpsdr", "Web888", "Web-888", "192.0.2.121", {"address": "192.0.2.121"})
    assert radios.free_name(tmp_path, second).name == "Web888_121"
    (tmp_path / "Web888_121.radio.json").write_text(json.dumps({"type": "flex", "address": "x"}))
    assert radios.free_name(tmp_path, second).name == "Web888_121_2"  # never another radio's file
    radios.record(tmp_path, radios.free_name(tmp_path, second))
    assert radios.free_name(tmp_path, second).name == "Web888_121_2"  # then its own
    flex = radios.Found("flex", "Web888", "FLEX-6600", "192.0.2.121", {"serial": "S1"})
    assert radios.free_name(tmp_path, flex) == flex  # a Flex is known by its serial


def test_an_hpsdr_radio_found_takes_the_operator_s_name(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pitaya = {**WEB888, "ip": "192.0.2.121", "board_id": 1, "gateware": 25, "receivers": 8}
    backends = {"flex": radios.Flex(lambda _s: {}), "hpsdr": _heard(pitaya)}
    monkeypatch.setattr(radios, "BACKENDS", backends)
    found = radios.discover(tmp_path, 1.0, None, "hpsdr", "Pitaya")
    assert found.startswith("Pitaya: HPSDR board 1 at 192.0.2.121, new")
    st = json.loads((tmp_path / "Pitaya.radio.json").read_text())
    assert (st["nickname"], st["address"], st["receivers"]) == ("Pitaya", "192.0.2.121", 8)
    assert "already set up as Pitaya" in radios.discover(tmp_path, 1.0, None, "hpsdr", "Shack")
    (tmp_path / "Lab.radio.json").write_text(json.dumps({"type": "hpsdr", "address": "192.0.2.50"}))
    for name, kind, error in (("Lab", "hpsdr", "another radio's name"),
                              ("Pit-2", "hpsdr", "names a receiver"),
                              ("Pitaya", "flex", "only an HPSDR radio"),
                              ("Web.888", "hpsdr", "a letter, then")):
        with pytest.raises(ValueError, match=error):
            radios.discover(tmp_path, 1.0, None, kind, name)
    monkeypatch.setitem(radios.BACKENDS, "hpsdr", _heard(pitaya, WEB888))
    with pytest.raises(ValueError, match="2 radios answered"):
        radios.discover(tmp_path, 1.0, None, "hpsdr", "Pitaya")


def test_discover_asks_only_the_type_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    class Fake:
        def __init__(self, kind: str, found: list[radios.Found]) -> None:
            self.type, self.found = kind, found
            self.capabilities = radios.Capabilities()

        def discover(self, seconds: float, address: str | None = None) -> list[radios.Found]:
            asked.append(f"{self.type} {address}")
            return self.found

    web = radios.Found("hpsdr", "Web888", "Web-888", "192.0.2.120", {"address": "192.0.2.120"},
                       note="in use by another client")
    fakes = {"flex": Fake("flex", []), "hpsdr": Fake("hpsdr", [web])}
    monkeypatch.setattr(radios, "BACKENDS", fakes)
    line = radios.discover(tmp_path, 1.0, None, "hpsdr")
    assert line == "Web888: Web-888 at 192.0.2.120, new (in use by another client)"
    assert asked == ["hpsdr None"]
    assert json.loads((tmp_path / "Web888.radio.json").read_text())["type"] == "hpsdr"
    radios.discover(tmp_path, 1.0, "192.0.2.150")  # no type: a FlexRadio, as before
    radios.discover(tmp_path, 1.0)
    assert asked == ["hpsdr None", "flex 192.0.2.150", "flex None"]
    for bad in ("kiwi", ["hpsdr"]):
        with pytest.raises(ValueError, match="radio type must be one of: flex, hpsdr"):
            radios.discover(tmp_path, 1.0, None, bad)  # type: ignore[arg-type]
