"""cdub89/SparkGap#32 R6, R7: the C receiver's minute capture (65 s of IQ per band) is taken when a
recording turns on, once per band, not at start; within a second of the RECORD file, not at the minute;
and freed when the recording stops, on every band only the recording used."""

from typing import Any

from sparkgap import SparkGap


class Lib:
    def __init__(self) -> None:
        self.enabled: list[int] = []
        self.freed: list[int] = []

    def hpsdr_enable_ft8(self, h: Any, rx: int, ft8_hz: Any, center_hz: Any) -> None:
        self.enabled.append(rx)

    def hpsdr_disable_ft8(self, h: Any, rx: int) -> None:
        self.freed.append(rx)


def _node(cfg: dict[str, Any]) -> tuple[Any, Lib]:
    sg: Any = SparkGap({"sdr_type": "hpsdr", "sample_rate": 192_000, "bands": ["40m", "60m"], **cfg})
    sg._band_meta = [("40m", 7_038_400, 0), ("60m", 5_000_000, 1)]   # 60m: no FT8 dial in its span
    lib = Lib()
    sg.receiver = type("R", (), {"lib": lib, "_h": None})()
    sg._iq_cap = set()
    return sg, lib


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


def test_the_recorder_queue_holds_a_minute_of_every_band(tmp_path: Any) -> None:
    from iq_recorder import MAX_QUEUE
    nine = ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "10m"]
    for bands, size in ((["40m"], MAX_QUEUE), (nine, 9)):
        sg: Any = SparkGap({"record_iq_dir": str(tmp_path), "bands": bands})
        assert sg._make_iq_recorder()._q.maxsize == size
    assert SparkGap({"bands": nine})._make_iq_recorder() is None


def test_recording_off_frees_only_the_bands_it_captured() -> None:
    sg, lib = _node({})                                # FT8 on: 40m keeps its capture
    sg._iq_cap = {0, 1}
    sg._disable_iq_capture()
    assert lib.freed == [1] and sg._iq_cap == {0}
    sg, lib = _node({"enable_ft8": False, "enable_rtty": False})
    sg._iq_cap = {0, 1}
    sg._disable_iq_capture()
    assert lib.freed == [0, 1] and sg._iq_cap == set()


def test_a_record_file_starts_the_capture_within_a_second(tmp_path: Any) -> None:
    from iq_recorder import BandRecorder
    sg, lib = _node({"enable_ft8": False, "enable_rtty": False})
    sg._iq_rec = BandRecorder(str(tmp_path), rate=1000)
    sg._poll_iq_record(30.0)
    assert lib.enabled == []
    (tmp_path / "RECORD").touch()
    sg._poll_iq_record(30.0)                           # the same second: not checked again
    assert lib.enabled == []
    sg._iq_rec_polled = None
    sg._poll_iq_record(57.0)                           # the minute's swap starts it: no sliver
    assert lib.enabled == []
    sg._poll_iq_record(31.0)
    assert lib.enabled == [0, 1]


def test_a_start_with_the_record_file_there_warns(tmp_path: Any, caplog: Any) -> None:
    from iq_recorder import BandRecorder
    (tmp_path / "RECORD").touch()
    with caplog.at_level("WARNING", logger="sparkgap"):
        BandRecorder(str(tmp_path), rate=1000)
    assert "IQ recording on at start" in caplog.text


def test_capture_only_on_the_bands_record_names(tmp_path: Any) -> None:
    """echo "40m" > RECORD: the minute capture (65 s of IQ) is taken on 40m only, not on 20m."""
    from iq_recorder import BandRecorder
    sg: Any = SparkGap({"sdr_type": "hpsdr", "sample_rate": 192_000, "bands": ["40m", "20m"]})
    sg._band_meta = [("40m", 7_038_400, 0), ("20m", 14_038_400, 1)]
    lib = Lib()
    sg.receiver = type("R", (), {"lib": lib, "_h": None})()
    sg._iq_cap = set()
    sg._iq_rec = BandRecorder(str(tmp_path), rate=1000)
    (tmp_path / "RECORD").write_text("40m\n")
    sg._enable_iq_capture()
    assert lib.enabled == [0] and sg._iq_cap == {0}
