"""cdub89/SparkGap#32 R6: the C receiver's minute capture (65 s of IQ per band) is taken when a
recording turns on, once per band, not at start."""

from typing import Any

from sparkgap import SparkGap


class Lib:
    def __init__(self) -> None:
        self.enabled: list[int] = []

    def hpsdr_enable_ft8(self, h: Any, rx: int, ft8_hz: Any, center_hz: Any) -> None:
        self.enabled.append(rx)


def test_capture_is_enabled_once_per_band_when_recording_starts() -> None:
    sg: Any = SparkGap({"sdr_type": "hpsdr", "sample_rate": 192_000, "bands": ["40m", "20m"]})
    sg._band_meta = [("40m", 7_038_400, 0), ("20m", 14_038_400, 1)]
    lib = Lib()
    sg.receiver = type("R", (), {"lib": lib, "_h": None})()
    sg._iq_cap = {0}                                   # 40m captures already (FT8)
    sg._enable_iq_capture()
    sg._enable_iq_capture()                            # the next minute: nothing new
    assert lib.enabled == [1] and sg._iq_cap == {0, 1}


def test_the_all_band_recorder_lights_rec(tmp_path: Any) -> None:
    """The status reports the all-band recorder while its RECORD file is there: Web shows REC."""
    from iq_recorder import BandRecorder
    sg: Any = SparkGap({"sdr_type": "hpsdr", "sample_rate": 192_000, "bands": ["40m"]})
    sg.receiver = type("R", (), {"health": lambda self: {"type": "hpsdr"}})()
    sg._iq_rec = BandRecorder(str(tmp_path), rate=1000)
    sg._wav_record = sg._wav_path = sg.telnet = None
    assert sg._gui_health(192_000)["recording"] is None
    (tmp_path / "RECORD").touch()
    assert sg._gui_health(192_000)["recording"] == {"file": str(tmp_path), "bytes": 0}
