"""A recording's info file: centre, rate, build and the config it ran with, plus live retunes."""

import json
from pathlib import Path
from types import SimpleNamespace

from sparkgap import SparkGap


def test_a_recording_gets_its_info_and_a_retune_is_added(tmp_path: Path) -> None:
    cfg = {"bands": ["20m"], "cw_decoder": "itila2", "spot_validation": "normal"}
    me = SimpleNamespace(cfg=cfg)
    wav = str(tmp_path / "Dallas-1_14038_20261104_194119Z.wav")
    SparkGap._write_rec_info(me, wav, 14_038_400, 96_000)  # pyright: ignore[reportArgumentType]
    info = json.loads((tmp_path / "Dallas-1_14038_20261104_194119Z.json").read_text())
    assert (info["center_hz"], info["sample_rate"], info["band"]) == (14_038_400, 96_000, "20m")
    assert info["config"] == cfg and info["start_utc"].endswith("Z")
    retune = {"utc": "2026-11-04T20:00:00Z", "band": "40m", "center_hz": 7_038_400}
    SparkGap._write_rec_info(me, wav, 7_038_400, 96_000, retune=retune)  # pyright: ignore[reportArgumentType]
    info = json.loads((tmp_path / "Dallas-1_14038_20261104_194119Z.json").read_text())
    assert info["retunes"] == [retune] and info["center_hz"] == 14_038_400
