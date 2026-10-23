"""The receive loop picks the right recovery, stop() wins over a recovery, and cleanup never
touches what the radio now gives another client (ids are reused)."""

import logging
import struct

import pytest

import flex_iq
from flex_iq import FlexIQReceiver, FlexStation
from tests.flex_fake_radio import STREAM_BASE, FakeRadio, use_fake_radio

Monkey = pytest.MonkeyPatch
Caplog = pytest.LogCaptureFixture


@pytest.fixture
def radio(monkeypatch: Monkey) -> FakeRadio:
    return use_fake_radio(monkeypatch)


def _live(radio: FakeRadio) -> FlexIQReceiver:
    FlexStation("192.0.2.1").start()
    rx = FlexIQReceiver("192.0.2.1", freq_hz=14_038_400, sample_rate=96_000, daxiq_channel=1)
    rx.start()
    return rx


class _Udp:
    def recvfrom(self, *_: object) -> tuple[bytes, tuple[str, int]]:
        head = struct.pack(">BBHI", 0x38, 0x01, 0, STREAM_BASE) + bytes(flex_iq.VITA_HEADER_SIZE - 8)
        return head + bytes(8 * 4), ("192.0.2.1", 4991)

    def close(self) -> None:
        pass


def _receive_until_recovery(monkeypatch: Monkey, rx: FlexIQReceiver, ready: list[str]) -> tuple[str, str]:
    """Drive receive() with select returning `ready` in turn ("tcp", "udp" or ""); the (why, kind)
    it recovers with."""
    rx._udp = _Udp()  # pyright: ignore[reportAttributeAccessIssue]
    turns = iter(ready)

    def select(*_: object) -> tuple[list[object], list[object], list[object]]:
        name = next(turns)
        return ([rx.conn if name == "tcp" else rx._udp] if name else []), [], []

    called: list[tuple[str, str]] = []

    def recover(why: str, kind: str) -> None:
        called.append((why, kind))
        rx._running = False

    monkeypatch.setattr(flex_iq.select, "select", select)
    monkeypatch.setattr(rx, "_recover", recover)
    rx.receive(lambda *_: None)
    assert len(called) == 1
    return called[0]


def test_a_closed_control_connection_reconnects(monkeypatch: Monkey, radio: FakeRadio) -> None:
    rx = _live(radio)
    assert rx.conn is not None
    rx.conn.lost = "The radio closed the control connection"
    assert _receive_until_recovery(monkeypatch, rx, ["tcp"]) == ("The radio closed the control connection", "link")


def test_a_stalled_stream_on_a_live_connection_gets_a_new_stream(monkeypatch: Monkey, radio: FakeRadio) -> None:
    rx = _live(radio)
    monkeypatch.setattr(flex_iq, "STALL_S", -1)
    why, kind = _receive_until_recovery(monkeypatch, rx, ["udp", ""])
    assert why.startswith("No DAX-IQ packets for -1 s") and kind == "stream"


def test_the_station_leaving_is_seen_at_once(monkeypatch: Monkey, radio: FakeRadio) -> None:
    rx = _live(radio)
    station = next(h for h, c in radio.clients.items() if c.gui)
    radio.drop(station)
    why, kind = _receive_until_recovery(monkeypatch, rx, ["tcp"])
    assert kind == "station" and "left the radio" in why


def test_no_recovery_once_stopping(radio: FakeRadio) -> None:
    rx = _live(radio)
    rx._stopping = True
    n = len(radio.log)
    rx._recover("No DAX-IQ packets for 10 s", "stream")
    assert len(radio.log) == n and rx.reconnects == 0


def test_stop_removes_our_objects_and_disconnects(radio: FakeRadio) -> None:
    rx = _live(radio)
    rx.stop()
    assert not radio.pans and not radio.slices and not radio.streams
    assert rx.conn is None and rx._stopping and not rx._running


def test_a_stream_id_another_client_now_has_is_left_alone(radio: FakeRadio) -> None:
    rx = _live(radio)
    assert rx._stream_id is not None
    radio.streams[rx._stream_id].client = 0x303A_65A4     # O2 got 0x2000000B after O1 removed it
    rx.stop()
    assert rx._stream_id is None and len(radio.streams) == 1


def test_a_pan_another_stream_feeds_is_left_alone(radio: FakeRadio) -> None:
    rx = _live(radio)
    pan = rx._pan_id
    assert pan is not None and rx._stream_id is not None
    radio.streams[rx._stream_id].client = 0x5F91_167C
    rx.stop()
    assert pan in radio.pans and len(radio.slices) == 1


def test_ptt_warning_once_per_connection(radio: FakeRadio, caplog: Caplog) -> None:
    rx = _live(radio)
    ptt = "S0|client 0x7F19D8AE connected local_ptt=1 program=SparkGap station=SparkGap-Dallas"
    assert rx.conn is not None
    with caplog.at_level(logging.WARNING, logger="sparkgap.flex"):
        for _ in range(2):
            rx.conn.queue.append(ptt)  # pyright: ignore[reportAttributeAccessIssue]
            rx._poll()
    assert sum("local PTT" in r.getMessage() for r in caplog.records) == 1


def test_no_disconnect_line_when_never_connected(caplog: Caplog) -> None:
    rx = FlexIQReceiver("192.0.2.1", sample_rate=96_000)
    with caplog.at_level(logging.INFO, logger="sparkgap.flex"):
        rx._close("failed start")
    assert not caplog.records and rx.conn is None
