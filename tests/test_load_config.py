"""sparkgap.load_config: a receiver of a radio file spots under that file's spotter ID and grid."""

import json
from collections.abc import Mapping
from pathlib import Path

from sparkgap import load_config


def _write(
    tmp_path: Path, receiver: Mapping[str, object], radio: Mapping[str, object] | None
) -> Path:
    if radio is not None:
        (tmp_path / "Dallas.radio.json").write_text(json.dumps(radio))
    path = tmp_path / "Dallas-1.local.json"
    path.write_text(json.dumps(receiver))
    return path


def test_spotter_id_and_grid_come_from_the_radio_file(tmp_path: Path) -> None:
    receiver = {"radio": "Dallas", "callsign": "OLD", "grid": "AA00", "skimmer_suffix": "-1"}
    cfg = load_config(str(_write(tmp_path, receiver, {"callsign": "N0CALL-5", "grid": "EM12ou"})))
    assert (cfg["callsign"], cfg["grid"], cfg["skimmer_suffix"]) == ("N0CALL-5", "EM12ou", "")


def test_without_a_radio_file_the_receivers_own_values_stay(tmp_path: Path) -> None:
    cfg = load_config(str(_write(tmp_path, {"radio": "Dallas", "callsign": "N0CALL"}, None)))
    assert cfg["callsign"] == "N0CALL"
    alone = {"callsign": "N0CALL", "grid": "EM12"}
    cfg = load_config(str(_write(tmp_path, alone, {"callsign": "X"})))
    assert (cfg["callsign"], cfg["grid"]) == ("N0CALL", "EM12")  # no radio key: not a node's


def test_an_hpsdr_receiver_takes_its_address_from_the_radio_file(tmp_path: Path) -> None:
    radio = {"type": "hpsdr", "address": "192.0.2.120", "callsign": "N0CALL-1", "grid": "EM12ub"}
    cfg = load_config(str(_write(tmp_path, {"radio": "Dallas", "sdr_ip": "192.0.2.9"}, radio)))
    assert (cfg["sdr_ip"], cfg["callsign"]) == ("192.0.2.120", "N0CALL-1")
    flex = {**radio, "type": "flex"}  # a Flex receiver finds its radio through flex_iq
    cfg = load_config(str(_write(tmp_path, {"radio": "Dallas", "sdr_ip": "192.0.2.9"}, flex)))
    assert cfg["sdr_ip"] == "192.0.2.9"
