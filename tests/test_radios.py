"""The back-end interface (docs/RADIOS.md): what radios each back end hears, and what its
receivers can change."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import radios

LAKE = {"nickname": "Lake", "model": "FLEX-6600", "ip": "192.0.2.101"}
INFO = {
    "192.0.2.101": {"model": "FLEX-6600", "name": "Lake", "chassis_serial": "3718-0522-6600-0003",
                    "callsign": "N0CALL", "num_scu": "2", "num_slice": "4",
                    "software_ver": "4.3.16"},
    "192.0.2.150": {"model": "FLEX-6400M", "name": "Dallas",
                    "chassis_serial": "0520-0945-6401-3114", "num_scu": "1", "num_slice": "2"},
}


def _ask(ip: str) -> dict[str, str]:
    if ip not in INFO:
        raise OSError("no route to host")
    return INFO[ip]


def test_flex_discovery_names_each_radio_by_its_nickname() -> None:
    heard = {"3718-0522-6600-0003": LAKE, "0520-0945-6401-3114": {"model": "FLEX-6400M",
                                                                   "ip": "192.0.2.150"}}
    found = radios.Flex(lambda _s: heard, ask=lambda _ip: {}).discover(1.0)
    assert [(r.name, r.model, r.address, r.identity) for r in found] == [
        ("FLEX-6400M-3114", "FLEX-6400M", "192.0.2.150", {"serial": "0520-0945-6401-3114"}),
        ("Lake", "FLEX-6600", "192.0.2.101", {"serial": "3718-0522-6600-0003"}),
    ]


def test_a_radio_found_again_keeps_the_operator_s_settings(tmp_path: Path) -> None:
    r = radios.Found("flex", "Lake", "FLEX-6600", "192.0.2.101", {"serial": "3718"})
    assert radios.record(tmp_path, r) == "Lake: FLEX-6600 at 192.0.2.101, new"
    path = tmp_path / "Lake.radio.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "callsign": "N0CALL-2"}))
    assert radios.record(tmp_path, r) == "Lake: FLEX-6600 at 192.0.2.101, unchanged"
    moved = radios.Found("flex", "Lake", "FLEX-6600", "192.0.2.102", {"serial": "3718"})
    assert radios.record(tmp_path, moved) == "Lake: FLEX-6600 at 192.0.2.102, was 192.0.2.101"
    assert json.loads(path.read_text()) == {"type": "flex", "nickname": "Lake",
                                            "model": "FLEX-6600", "serial": "3718",
                                            "address": "192.0.2.102", "callsign": "N0CALL-2"}


def test_a_name_that_cannot_be_a_file_name_is_skipped(tmp_path: Path) -> None:
    r = radios.Found("flex", "../Lake", "FLEX-6600", "192.0.2.101")
    assert "skipped" in radios.record(tmp_path, r)
    assert not list(tmp_path.iterdir())


def test_what_a_receiver_can_change_comes_from_its_back_end() -> None:
    flex = radios.capabilities("flex")
    assert flex.station and flex.live_band and "antenna" in flex.rx_settings
    other = radios.capabilities("hpsdr")  # no back end yet: nothing beyond band and options
    assert (other.station, other.live_band, other.rx_settings) == (False, False, ())


def test_discover_by_address_records_what_the_radio_holds() -> None:
    found = radios.Flex(lambda _s: {}, ask=_ask).discover(1.0, "192.0.2.150")
    assert [(r.name, r.model, r.identity, r.facts) for r in found] == [
        ("Dallas", "FLEX-6400M", {"serial": "0520-0945-6401-3114"},
         {"num_scu": 1, "max_slices": 2, "max_panadapters": 2, "daxiq_channels": 2})]


def test_discover_by_address_says_when_nothing_answers() -> None:
    with pytest.raises(RuntimeError, match="no FlexRadio answers at 192.0.2.9"):
        radios.Flex(ask=_ask).discover(1.0, "192.0.2.9")


def test_facts_merge_into_the_radio_file_and_the_node_settings_stay(tmp_path: Path) -> None:
    path = tmp_path / "Lake.radio.json"
    path.write_text(json.dumps({"nickname": "Lake", "callsign": "N0CALL-2", "grid": "AA00aa"}))
    (r,) = radios.Flex(lambda _s: {"3718-0522-6600-0003": LAKE}, ask=_ask).discover(1.0)
    radios.record(tmp_path, r)
    st = json.loads(path.read_text())
    # the radio's own callsign is not taken
    assert (st["callsign"], st["grid"]) == ("N0CALL-2", "AA00aa")
    assert (st["num_scu"], st["max_panadapters"], st["daxiq_channels"]) == (2, 4, 4)
    assert radios.receiver_limit(st) == 4


def test_the_receiver_limit_is_the_smallest_of_pans_slices_and_channels() -> None:
    assert radios.receiver_limit({"max_panadapters": 4, "max_slices": 4, "daxiq_channels": 2}) == 2
    assert radios.receiver_limit({"model": "FLEX-6600"}) is None


def test_a_radio_renamed_on_the_radio_does_not_get_a_second_file(tmp_path: Path) -> None:
    old = tmp_path / "Dallas.radio.json"
    old.write_text(json.dumps({"nickname": "Dallas", "serial": "S1", "callsign": "N0CALL-5"}))
    r = radios.Found("flex", "Shack", "FLEX-6400M", "192.0.2.150", {"serial": "S1"})
    assert "Dallas.radio.json holds (renamed on the radio), skipped" in radios.record(tmp_path, r)
    assert not (tmp_path / "Shack.radio.json").exists()


def _node_root(tmp_path: Path) -> Path:
    lake = {"nickname": "Lake", "model": "FLEX-6600"}
    (tmp_path / "Lake.radio.json").write_text(json.dumps(lake))
    for name, radio in (("Lake-3", "Lake"), ("Dallas-1", "Dallas")):
        rx = {"radio": radio, "callsign": "N0CALL"}
        (tmp_path / f"{name}.local.json").write_text(json.dumps(rx))
    return tmp_path


def test_set_node_writes_the_radio_and_its_receivers_read_it(tmp_path: Path) -> None:
    lines, receivers = radios.set_node(_node_root(tmp_path), "Lake", "n0call-2", "em12UB", " Op ")
    st = json.loads((tmp_path / "Lake.radio.json").read_text())
    node = (st["callsign"], st["grid"], st["op_name"], st["rbn_upload"])
    assert node == ("N0CALL-2", "EM12ub", "Op", "dry")
    assert receivers == ["Lake-3"] and len(lines) == 2
    assert "callsign" not in json.loads((tmp_path / "Lake-3.local.json").read_text())
    assert json.loads((tmp_path / "Dallas-1.local.json").read_text())["callsign"] == "N0CALL"
    again = radios.set_node(tmp_path, "Lake", "N0CALL-2", "EM12ub", "Op")
    assert again == (["Lake: unchanged"], [])


@pytest.mark.parametrize("args, why", [
    (("Lake", "not a call", "EM12", "Op"), "a callsign"),
    (("Lake", "N0CALL-2", "EM1", "Op"), "a 4 or 6 character grid"),
    (("Lake", "N0CALL-2", "EM12", " "), "operator's name"),
    (("Shack", "N0CALL-2", "EM12", "Op"), "find the radio first"),
    (("../Lake", "N0CALL-2", "EM12", "Op"), "not a radio name"),
])
def test_set_node_refuses_and_writes_nothing(tmp_path: Path, args: tuple[str, ...],
                                             why: str) -> None:
    root = _node_root(tmp_path)
    before = {p.name: p.read_text() for p in root.iterdir()}
    with pytest.raises(ValueError, match=why):
        radio, call, grid, op = args
        radios.set_node(root, radio, call, grid, op)
    assert {p.name: p.read_text() for p in root.iterdir()} == before


def test_radio_files_lists_every_radio(tmp_path: Path) -> None:
    root = _node_root(tmp_path)
    dallas = {"model": "FLEX-6400M", "daxiq_channels": 2}
    (root / "Dallas.radio.json").write_text(json.dumps(dallas))
    assert [(r["name"], r["model"], r["daxiq_channels"]) for r in radios.radio_files(root)] == [
        ("Dallas", "FLEX-6400M", 2), ("Lake", "FLEX-6600", None)]
