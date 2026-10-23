"""flex_station.py holds the station, writes its health and exits with a code the launcher reads."""

import json
import logging
from pathlib import Path

import pytest

import flex_iq
import flex_station
from flex_iq import FlexStation
from tests.flex_fake_radio import use_fake_radio

Monkey = pytest.MonkeyPatch


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "seat.local.json"
    cfg.write_text(json.dumps({"sdr_type": "flex", "sdr_ip": "192.0.2.1", "sdr_port": 4992}))
    return cfg


def test_it_holds_the_station_writes_health_and_stops_cleanly(
    monkeypatch: Monkey, tmp_path: Path
) -> None:
    radio = use_fake_radio(monkeypatch)
    monkeypatch.setattr(flex_iq, "HEARTBEAT_S", 0)
    beats = [0]
    real_beat = FlexStation._beat

    def beat(self: FlexStation) -> str | None:
        beats[0] += 1
        if beats[0] == 3:
            self.request_stop()
        return real_beat(self)

    monkeypatch.setattr(FlexStation, "_beat", beat)
    status = tmp_path / "station.json"
    assert flex_station.main(["--config", str(_config(tmp_path)), "--status", str(status)]) == 0
    health = json.loads(status.read_text())
    assert health["station"] == "SparkGap-Dallas" and health["state"] == "stopped"
    assert health["rtt_ms"] is not None and not radio.clients


def test_it_leaves_a_station_another_process_holds(monkeypatch: Monkey, tmp_path: Path) -> None:
    use_fake_radio(monkeypatch)
    FlexStation("192.0.2.1").start()
    assert flex_station.main(["--config", str(_config(tmp_path))]) == flex_station.EXIT_EXISTS


def test_it_refuses_a_config_that_is_not_a_flex(tmp_path: Path) -> None:
    cfg = tmp_path / "hpsdr.local.json"
    cfg.write_text(json.dumps({"sdr_type": "hpsdr", "sdr_ip": "192.0.2.1"}))
    with pytest.raises(SystemExit, match="not a Flex config"):
        flex_station.main(["--config", str(cfg)])


def test_a_status_file_that_cannot_be_written_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A full disk must not stop the station and every copy bound to it (audit 2026-10-22)."""
    missing = tmp_path / "gone" / "station.json"
    with caplog.at_level(logging.INFO, logger="sparkgap.flex"):
        flex_station.write_status(missing, {"state": "live"})
        flex_station.write_status(missing, {"state": "live"})
        missing.parent.mkdir()
        flex_station.write_status(missing, {"state": "live"})
    assert caplog.text.count("not written") == 1 and "written to" in caplog.text
    assert json.loads(missing.read_text()) == {"state": "live"}
