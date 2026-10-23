"""flex_iq parsers and the control connection: owned objects, GUI stations, replies, status reads."""

import pytest

from flex_iq import FlexConnection, FlexIQReceiver, gui_stations, owned_objects, pan_slices, station_for
from tests.flex_fake_radio import use_fake_radio

STATUS = [
    # SparkGap's default objects (the radio's Slice A on the 40m phone default)
    "S3CBB9746|slice 1 in_use=1 RF_frequency=7.175000 client_handle=0x3CBB9746 dax=1",
    "S3CBB9746|display pan 0x40000000 center=7.175000 client_handle=0x3CBB9746",
    # another station (SmartSDR with CW Skimmer): must never be touched
    "S3CBB9746|slice 0 in_use=1 RF_frequency=7.053000 client_handle=0x1A2B3C4D dax_iq=1",
    "S3CBB9746|display pan 0x40000002 center=7.053000 client_handle=0x1A2B3C4D",
    # a freed slice slot still reported for our handle
    "S3CBB9746|slice 2 in_use=0 client_handle=0x3CBB9746",
    "R5|0|",
]


def test_only_own_objects_are_returned() -> None:
    assert owned_objects(STATUS, "3CBB9746") == ({1}, {"0x40000000"})


def test_handle_prefix_of_another_handle_does_not_match() -> None:
    line = "S1|slice 3 in_use=1 client_handle=0x3CBB97461"
    assert owned_objects([line], "3CBB9746") == (set(), set())


def test_no_handle_frees_nothing() -> None:
    assert owned_objects(STATUS, "") == (set(), set())


def test_cmd_returns_at_its_own_reply() -> None:
    import socket
    import time

    conn = FlexConnection("127.0.0.1")
    ours, radio = socket.socketpair()
    conn._tcp = ours
    radio.sendall(b"S1|slice 0 in_use=1\nR0|0|\nR1|0|7\nS1|display pan 0x40000000\n")
    t0 = time.time()
    lines = conn.cmd("slice create", timeout=2.0)
    assert time.time() - t0 < 0.5
    assert lines[-1] == "R1|0|7" and conn.hex_id(lines) == 7
    assert conn.cmd("sub pan all", timeout=0.3, settle=0.1)[-1] == "S1|display pan 0x40000000"
    ours.close()
    radio.close()


CLIENTS = [
    "S3BEDC9D2|client 0x73E33F06 connected client_id=8A1B6F0E-0000-4000-8000-000000000001 "
    "program=SmartSDR-Win station=BEELINK local_ptt=1",
    "S3BEDC9D2|client 0x1EFAD774 connected client_id=5C2D7A11-0000-4000-8000-000000000002 "
    "program=SparkGap station=SparkGap-2 local_ptt=0",
    "S3BEDC9D2|client 0x3BEDC9D2 connected program=SparkGap station=",     # non-GUI: no client_id
    "R7|0|",
]


def test_gui_stations_lists_clients_with_an_id() -> None:
    assert gui_stations(CLIENTS) == [
        ("0x73E33F06", "8A1B6F0E-0000-4000-8000-000000000001", "SmartSDR-Win", "BEELINK"),
        ("0x1EFAD774", "5C2D7A11-0000-4000-8000-000000000002", "SparkGap", "SparkGap-2"),
    ]


def test_pan_slices_finds_slices_on_one_pan_whoever_owns_them() -> None:
    lines = [
        "S1|slice 0 in_use=1 pan=0x40000000 client_handle=0x73E33F06",
        "S1|slice 2 in_use=1 pan=0x40000002 client_handle=0x1EFAD774",
        "S1|slice 3 in_use=0 pan=0x40000002 client_handle=0x1EFAD774",
    ]
    assert pan_slices(lines, 0x40000002) == {2}
    assert pan_slices(lines, 0x40000003) == set()


def test_clients_gone_picks_the_disconnect_lines() -> None:
    from flex_iq import clients_gone

    gone = "S1|client 0x39795134 disconnected forced=1"
    assert clients_gone(CLIENTS + [gone]) == [gone]


def test_ptt_ours_only_when_a_sparkgap_station_holds_local_ptt() -> None:
    from flex_iq import ptt_ours

    ours = "S1|client 0x1372089D connected local_ptt=1 program=SparkGap station=SparkGap-2"
    lines = ["S1|client 0x26DB5920 connected local_ptt=1 program=SmartSDR-Win station=BEELINK",
             "S1|client 0x1372089D connected local_ptt=0 program=SparkGap station=SparkGap-2"]
    assert ptt_ours(lines) == []
    assert ptt_ours(lines + [ours]) == [ours]


def test_poll_empties_the_connection_and_sees_it_close() -> None:
    import socket

    conn = FlexConnection("127.0.0.1")
    conn._tcp, radio = socket.socketpair()
    conn._tcp.settimeout(2.0)
    radio.sendall(b"S1|interlock state=TRANSMITTING\n" * 2_000 + b"S1|slice 0 tx")
    while conn._buf != b"S1|slice 0 tx":     # every whole line read and dropped, the part line kept
        assert conn.poll() is not None
    assert conn.poll() == []                 # nothing waiting: still connected
    radio.close()
    assert conn.poll() is None
    assert conn.lost == "The radio closed the control connection"
    conn.close("stopping")
    assert conn._tcp is None


def test_one_station_per_radio_named_after_its_nickname() -> None:
    assert station_for("Dallas") == "SparkGap-Dallas"
    assert station_for("Lake House") == "SparkGap-Lake-House"
    assert station_for("") == "SparkGap"
    assert FlexIQReceiver("0.0.0.0", daxiq_channel=2).station == "SparkGap"
    assert FlexIQReceiver("0.0.0.0", daxiq_channel=2, station="SparkGap-1").station == "SparkGap-1"


def test_a_failed_start_removes_the_pan_and_slice_it_made(monkeypatch: pytest.MonkeyPatch) -> None:
    radio = use_fake_radio(monkeypatch)
    radio.refuse["stream create"] = "50000005"
    rx = FlexIQReceiver("192.0.2.1", freq_hz=7_038_400, sample_rate=96_000, daxiq_channel=3)
    with pytest.raises(RuntimeError, match="DAX-IQ stream"):
        rx.start()
    assert not radio.pans and not radio.slices and rx.conn is None
