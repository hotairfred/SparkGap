"""flex_station.py holds the station, writes its health and exits with a code the launcher reads."""

import json
from pathlib import Path
from typing import Any

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
    sent: list[dict[str, Any]] = []

    class Link:
        def __init__(self, role: str, name: str, on_command: Any) -> None:
            assert (role, name) == ("station", "192.0.2.1")

        def start(self) -> "Link":
            return self

        def status(self, **h: Any) -> None:
            sent.append(h)

        def close(self) -> None:
            pass

    monkeypatch.setattr(flex_station, "ProcessLink", Link)
    assert flex_station.main(["--config", str(_config(tmp_path))]) == 0
    health = sent[-1]
    assert len(sent) >= 3 and health["station"] == "SparkGap-Dallas"
    assert health["state"] == "stopped"
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

