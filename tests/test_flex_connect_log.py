"""flex_iq logs every connection attempt and command, and refuses a DAX-IQ channel another client uses
(status lines captured on the Lakehouse 6600, Fred's 6700 and the Dallas 6400M)."""

import logging

import pytest

import flex_iq
from flex_iq import FlexConnection, FlexIQReceiver, FlexStation
from tests.flex_fake_radio import use_fake_radio

Monkey = pytest.MonkeyPatch
Caplog = pytest.LogCaptureFixture
ATTEMPT = "[Flex] Connection attempt {} to 192.0.2.1:4992 ({})"
FAILED = "[Flex] Connection attempt {} failed: connection refused"
LAKE_STATION = 0x79D6_0253


def _lines(caplog: Caplog) -> list[tuple[int, str]]:
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "sparkgap.flex"]


class _ResetSocket:
    def recv(self, *_: object) -> bytes:
        raise ConnectionResetError(104, "Connection reset by peer")

    def sendall(self, *_: object) -> None:
        pass

    def close(self) -> None:
        pass


def test_last_packet_time_reads_as_utc() -> None:
    assert flex_iq.utc_hms(1_792_376_071.0) == "02:14:31Z"
    assert flex_iq.utc_hms(None) == "none since connecting"


def test_reply_names_its_command_and_flags_refusals() -> None:
    sent = {7: "client gui", 12: "slice set 0 audio_mute=1"}
    ok = "[Flex] <<< R12|0| (slice set 0 audio_mute=1)"
    assert flex_iq.reply_log("R12|0|", sent) == (logging.INFO, ok)
    refused = flex_iq.reply_log("R7|F3000001|Unable to create GUI client", sent)
    text = "[Flex] <<< R7|F3000001|Unable to create GUI client (client gui)"
    assert refused == (logging.WARNING, text)
    assert sent == {}
    assert flex_iq.reply_log("R99|0|", sent) == (logging.INFO, "[Flex] <<< R99|0| (?)")
    assert flex_iq.reply_log("S1|slice 0 tx=0", sent) is None


class _Stop(Exception):
    pass


MAESTRO = ("S0|client 0x28B5BE40 connected client_id=CCCC-3 program=SmartSDR-Maestro "
           "station=MaestroC")
LAKE_GUI = "S0|client 0x79D60253 connected client_id=AAAA-1 program=SparkGap station=SparkGap-Lake"
LAKE_PANS = [  # Lake 6600, 2026-10-19: Maestro's pan holds DAX-IQ 1, SparkGap-Lake's hold 2 and 3
    "S46CAE6B4|display pan 0x40000000 client_handle=0x28B5BE40 wnb=0 center=7.074000",
    "S46CAE6B4|display pan 0x40000000 daxiq_channel=1 ",
    "S46CAE6B4|display pan 0x40000001 client_handle=0x79D60253 wnb=0 center=14.038400",
    "S46CAE6B4|display pan 0x40000001 daxiq_channel=2 ",
    "S46CAE6B4|display pan 0x40000002 client_handle=0x79D60253 wnb=0 center=10.138400",
    "S46CAE6B4|display pan 0x40000002 daxiq_channel=3 ",
]


class _Stop(Exception):
    pass


class _Scripted(FlexConnection):
    """A bound channel's connection that answers from captured status and stops at the pan."""

    def __init__(self, status: dict[str, list[str]]) -> None:
        super().__init__("192.0.2.1")
        self.handle, self.status, self.sent = "7F19D8AE", status, []

    def cmd(self, cmd: str, timeout: float = 2.0, settle: float = 0.0) -> list[str]:
        if cmd.startswith("display pan"):
            raise _Stop
        self.seq += 1
        self.sent.append(cmd)
        return self.status.get(cmd, []) + [f"R{self.seq}|0|"]


def _build_to_pan(channel: int, clients: list[str], pans: list[str]) -> list[str]:
    """Commands a channel bound to SparkGap-Lake sends until it makes its pan."""
    rx = FlexIQReceiver("192.0.2.1", sample_rate=96_000, daxiq_channel=channel)
    conn = _Scripted({"sub pan all": pans, "sub daxiq all": pans})
    rx.conn, rx._clients, rx._station_handle = conn, clients, LAKE_STATION
    with pytest.raises(_Stop):
        rx._build()
    return conn.sent


def test_daxiq_holders_reads_pan_status() -> None:
    assert flex_iq.daxiq_holders(LAKE_PANS, 1) == {"0x40000000": "0X28B5BE40"}
    assert flex_iq.daxiq_holders(LAKE_PANS, 3) == {"0x40000002": "0X79D60253"}
    assert flex_iq.daxiq_holders(LAKE_PANS, 4) == {}
    assert flex_iq.daxiq_holders(["S0|display pan 0x40000003 daxiq_channel=0 "], 0) == {}


def test_a_channel_another_station_holds_is_refused(monkeypatch: Monkey) -> None:
    with pytest.raises(RuntimeError, match=r"DAX-IQ 1 is in use by MaestroC \(SmartSDR-Maestro\) "
                                           r"on pan 0x40000000"):
        _build_to_pan(1, [MAESTRO, LAKE_GUI], LAKE_PANS)


def test_a_channel_another_copy_of_our_station_streams_is_refused(monkeypatch: Monkey) -> None:
    live = LAKE_PANS + ["S79D60253|stream 0x20000011 type=dax_iq daxiq_channel=3 pan=0x40000002 "
                        "client_handle=0x79D60253 active=1"]
    with pytest.raises(RuntimeError, match=r"DAX-IQ 3 is in use by SparkGap-Lake \(SparkGap\)"):
        _build_to_pan(3, [MAESTRO, LAKE_GUI], live)


def test_a_channel_a_bound_copy_streams_names_its_station_and_pan(monkeypatch: Monkey) -> None:
    # Dallas, 2026-10-20 22:20Z: the bound copy's stream has its own handle; the station is its GUI handle
    live = LAKE_PANS + ["S6B976AE3|stream 0x20000011 type=dax_iq daxiq_channel=3 pan=0x40000002 "
                        "client_handle=0x6B976AE3 client_gui_handle=0x79D60253 active=1"]
    with pytest.raises(RuntimeError, match=r"DAX-IQ 3 is in use by SparkGap-Lake \(SparkGap\) on pan 0x40000002;"):
        _build_to_pan(3, [MAESTRO, LAKE_GUI], live)


def test_a_free_channel_goes_ahead(monkeypatch: Monkey) -> None:
    sent = _build_to_pan(4, [MAESTRO, LAKE_GUI], LAKE_PANS)
    assert sent[-1] == "sub tx all"


def test_an_unknown_owner_is_named_by_handle(monkeypatch: Monkey) -> None:
    with pytest.raises(RuntimeError,
                       match=r"DAX-IQ 1 is in use by client 0X28B5BE40 on pan 0x40000000"):
        _build_to_pan(1, [LAKE_GUI], LAKE_PANS)


Y_STREAM = [  # Y.log 2026-10-20: the owner left, the stream lost its pan, then a restored pan took it
    "S1A13B591|stream 0x20000011 type=dax_iq daxiq_channel=4 pan=0x0 slice=0x0 endpoint_type=Not Assigned "
    "daxiq_rate=96000 client_handle=0x5F91167C client_gui_handle=0x1A13B591 active=1 ip=192.168.0.100",
    "S303A65A4|stream 0x20000011 type=dax_iq daxiq_channel=4 pan=0x40000001 slice=0x0 endpoint_type=Display "
    "daxiq_rate=96000 client_handle=0x5F91167C client_gui_handle=0x303A65A4 active=1 ip=192.168.0.100",
    "S493016BC|stream 0x20000000 type=dax_iq active=0 ",
    "S493016BC|stream 0x2000000A removed ",
]


def test_daxiq_streams_reads_stream_status() -> None:
    streams = flex_iq.daxiq_streams(Y_STREAM)
    assert streams == {0x2000_0011: {"channel": "4", "pan": 0x4000_0001, "client": 0x5F91_167C, "gui": 0x303A_65A4}}
    assert flex_iq.daxiq_streams(Y_STREAM + ["S5F91167C|stream 0x20000011 removed "]) == {}
    assert flex_iq.daxiq_users(streams, 4, 0x5F91_167C, set()) == set()
    assert flex_iq.daxiq_users(streams, 4, 0x7F19_D8AE, set()) == {0x5F91_167C}


def test_an_inactive_daxiq_stream_still_holds_its_channel() -> None:
    # Maestro's DAX keeps its channel 1 streams with active=0 between uses (O1.log 2026-10-20)
    line = ("S1FCCB95D|stream 0x20000004 type=dax_iq daxiq_channel=1 pan=0x40000000 slice=0x0 "
            "endpoint_type=Display daxiq_rate=48000 client_handle=0x493016BC active=0")
    assert flex_iq.daxiq_users(flex_iq.daxiq_streams([line]), 1, 0x7F19_D8AE, set()) == {0x4930_16BC}


AETHER_GUI = "S0|client 0x6EDCDBAA connected client_id=DDDD-4 program=AetherSDR station=Fred6700"
AETHER_TCI = [  # Fred's 6700, 2026-10-20 17:5xZ: a TCI server keeps a stream on every channel, binds only one
    "S56B13ECD|stream 0x20000000 type=dax_iq daxiq_channel=1 pan=0x0 endpoint_type=Not Assigned daxiq_rate=192000 "
    "client_handle=0x56B13ECD client_gui_handle=0x6EDCDBAA active=1",
    "S56B13ECD|stream 0x20000001 type=dax_iq daxiq_channel=2 pan=0x40000000 endpoint_type=Display daxiq_rate=48000 "
    "client_handle=0x56B13ECD client_gui_handle=0x6EDCDBAA active=1",
    "S56B13ECD|stream 0x20000002 type=dax_iq daxiq_channel=3 pan=0x0 endpoint_type=Not Assigned daxiq_rate=48000 "
    "client_handle=0x56B13ECD client_gui_handle=0x6EDCDBAA active=1",
    "S56B13ECD|stream 0x20000003 type=dax_iq daxiq_channel=4 pan=0x0 endpoint_type=Not Assigned daxiq_rate=48000 "
    "client_handle=0x56B13ECD client_gui_handle=0x6EDCDBAA active=1",
]


def test_only_a_bound_stream_or_a_sparkgap_copys_claims_a_channel() -> None:
    streams = flex_iq.daxiq_streams(AETHER_TCI)
    me = 0x7F19_D8AE
    assert [flex_iq.daxiq_users(streams, c, me, set()) for c in (1, 2, 3, 4)] == [set(), {0x56B1_3ECD}, set(), set()]
    assert flex_iq.daxiq_users(streams, 3, me, {0x6EDC_DBAA}) == {0x56B1_3ECD}


def test_a_channel_another_program_streams_unbound_goes_ahead(monkeypatch: Monkey) -> None:
    # the radio let a copy stream channel 3 next to the TCI server's unbound stream for 30 min (Fred, 10-20)
    sent = _build_to_pan(3, [AETHER_GUI, LAKE_GUI], LAKE_PANS + AETHER_TCI)
    assert sent[-1] == "sub tx all"      # on to its own pan


def test_a_channel_another_program_binds_is_refused(monkeypatch: Monkey) -> None:
    with pytest.raises(RuntimeError, match=r"DAX-IQ 2 is in use by Fred6700 \(AetherSDR\) on pan 0x40000000;"):
        _build_to_pan(2, [AETHER_GUI, LAKE_GUI], LAKE_PANS + AETHER_TCI)


def test_a_channel_another_copy_streams_before_its_bind_is_refused(monkeypatch: Monkey) -> None:
    # a bound copy's new stream has no pan until it binds; its station's GUI client is SparkGap
    live = LAKE_PANS + ["S6B976AE3|stream 0x20000011 type=dax_iq daxiq_channel=3 pan=0x0 "
                        "client_handle=0x6B976AE3 client_gui_handle=0x79D60253 active=1"]
    with pytest.raises(RuntimeError, match=r"DAX-IQ 3 is in use by SparkGap-Lake \(SparkGap\) on pan none;"):
        _build_to_pan(3, [MAESTRO, LAKE_GUI], live)

def test_pan_status_keeps_saved_channels_and_drops_removed_pans() -> None:
    lines = LAKE_PANS + ["S0|display pan 0x40000003 center=7.038400", "S0|display pan 0x40000001 removed"]
    assert flex_iq.pan_status(lines) == {0x4000_0000: "1", 0x4000_0002: "3", 0x4000_0003: None}


TX_LINES = [  # Lakehouse 6600, 2026-10-21 17:45:31Z: VVV on the radio's own keyer (break-in)
    "S0|interlock tx_client_handle=0x3B49B6BD state=PTT_REQUESTED reason= source=HWCW",
    "S0|interlock tx_client_handle=0x3B49B6BD state=TRANSMITTING reason= source=HWCW",
    "S3B49B6BD|interlock acc_txreq_enable=0 rca_txreq_enable=0 acc_tx_enabled=1 tx1_enabled=1",
    "S0|interlock tx_client_handle=0x00000000 state=UNKEY_REQUESTED reason= source=",
    "S0|interlock tx_client_handle=0x00000000 state=READY reason= source=",
]


def test_tx_states_reads_the_interlock_state() -> None:
    assert flex_iq.tx_states(TX_LINES) == ["PTT_REQUESTED", "TRANSMITTING", "UNKEY_REQUESTED", "READY"]


def test_transmitting_at_covers_key_down_to_key_up_and_a_hold() -> None:
    rx = FlexIQReceiver("192.0.2.1")
    for state, t in zip(flex_iq.tx_states(TX_LINES), (100.0, 100.02, 101.0, 101.05)):
        rx._note_tx(state, t)
    assert not rx.transmitting_at(99.9)
    assert rx.transmitting_at(100.5) and rx.transmitting_at(101.05 + flex_iq.TX_HOLD_S)
    assert not rx.transmitting_at(101.05 + flex_iq.TX_HOLD_S + 0.01)
    rx._note_tx("PTT_REQUESTED", 102.0)                 # keyed again, not yet up
    assert rx.transmitting_at(102.5) and not rx.transmitting_at(101.5)


def _lines_at(caplog: Caplog, level: int) -> list[str]:
    return [m for lv, m in _lines(caplog) if lv >= level]


def test_a_start_logs_the_attempt_and_a_failure_logs_an_error(monkeypatch: Monkey, caplog: Caplog) -> None:
    radio = use_fake_radio(monkeypatch)
    FlexStation("192.0.2.1").start()
    radio.refuse["stream create"] = "50000005"
    rx = FlexIQReceiver("192.0.2.1", sample_rate=96_000)
    with caplog.at_level(logging.INFO, logger="sparkgap.flex"), pytest.raises(RuntimeError):
        rx.start()
    assert ATTEMPT.format(1, "first start") in [m for _, m in _lines(caplog)]
    assert _lines_at(caplog, logging.ERROR) == ["[Flex] Connection attempt 1 failed: [Flex] Failed to create DAX-IQ stream"]


def test_a_spent_budget_stops_sparkgap(monkeypatch: Monkey, caplog: Caplog) -> None:
    use_fake_radio(monkeypatch)
    kills: list[int] = []
    monkeypatch.setattr(flex_iq.os, "kill", lambda _pid, sig: kills.append(sig))
    FlexStation("192.0.2.1").start()
    rx = FlexIQReceiver("192.0.2.1", sample_rate=96_000)
    rx.start()
    rx.budget = flex_iq.LossBudget(budget=0)
    with caplog.at_level(logging.INFO, logger="sparkgap.flex"):
        rx._recover("No DAX-IQ packets for 10 s", "stream")
    assert "loss 1 within 60 min: stopping SparkGap" in _lines_at(caplog, logging.ERROR)[-1]
    assert kills == [flex_iq.signal.SIGINT] and rx.state == "halted"


def test_refused_setup_steps_fail_the_start(monkeypatch: Monkey) -> None:
    for refused in ("display pan set 0x40000000 center=", "slice tune", "display pan set 0x40000000 daxiq_channel="):
        radio = use_fake_radio(monkeypatch)
        FlexStation("192.0.2.1").start()
        radio.refuse[refused] = "50000029"
        rx = FlexIQReceiver("192.0.2.1", sample_rate=96_000, daxiq_channel=4)
        with pytest.raises(RuntimeError, match="refused"):
            rx.start()
        assert rx.state != "live" and not radio.pans and not radio.streams, refused


class _ResetSocket:
    def recv(self, *_: object) -> bytes:
        raise ConnectionResetError(104, "Connection reset by peer")

    def sendall(self, *_: object) -> None:
        pass

    def close(self) -> None:
        pass


def test_connection_error_keeps_its_cause() -> None:
    conn = FlexConnection("192.0.2.1")
    conn._tcp = _ResetSocket()  # pyright: ignore[reportAttributeAccessIssue]
    assert conn.poll() is None
    assert conn.lost == "Control connection error: [Errno 104] Connection reset by peer"


def test_own_disconnect_names_the_client_and_reason(caplog: Caplog) -> None:
    conn = FlexConnection("192.0.2.1")
    conn._tcp, conn.handle = _ResetSocket(), "290AB860"  # pyright: ignore[reportAttributeAccessIssue]
    with caplog.at_level(logging.INFO, logger="sparkgap.flex"):
        conn.close("reconnecting")
        conn.close("stopping")          # already closed: no second line
    line = "[Flex] Disconnected from 192.0.2.1 (client 0x290AB860, reconnecting)"
    assert _lines(caplog) == [(logging.INFO, line)]


def test_sent_commands_are_logged_at_info_and_pings_quietly(caplog: Caplog) -> None:
    import socket

    conn = FlexConnection("192.0.2.1")
    conn._tcp, radio = socket.socketpair()
    with caplog.at_level(logging.INFO, logger="sparkgap.flex"):
        radio.sendall(b"R1|0|\nR2|0|\n")
        conn.cmd("sub slice all", timeout=1.0)
        conn.cmd("ping", timeout=1.0)
    assert _lines(caplog) == [(logging.INFO, "[Flex] >>> C1|sub slice all"),
                              (logging.INFO, "[Flex] <<< R1|0| (sub slice all)")]
    radio.close()
    conn.close("stopping")


def test_close_says_goodbye_first() -> None:
    import socket

    conn = FlexConnection("192.0.2.1")
    conn._tcp, radio = socket.socketpair()
    conn.close("stopping")
    radio.settimeout(1.0)
    assert radio.recv(16) == b"\x04"
    radio.close()


DALLAS_INFO = ('R1|0|model="FLEX-6400M",chassis_serial="0520-0945-6401-3114",name="Dallas",'
               'callsign="N0CALL",gps="Not Present",atu_present=1,num_scu=1')


def test_the_station_is_named_after_the_radio_nickname() -> None:
    assert flex_iq.radio_nickname([DALLAS_INFO]) == "Dallas"
    assert flex_iq.radio_nickname(['R1|0|model="FLEX-6600",name=""']) == ""
    assert flex_iq.radio_nickname(["R1|50000016|"]) == ""


def test_the_station_client_id_is_the_same_on_every_start_and_differs_by_radio(monkeypatch: Monkey) -> None:
    def client_id(nickname: str) -> str:
        use_fake_radio(monkeypatch, nickname=nickname)
        st = FlexStation("192.0.2.1")
        st.start()
        return st.client_id

    a, b, other = client_id("Lake"), client_id("Lake"), client_id("Dallas")
    assert a == b != other
    assert a == a.upper() and len(a) == 36
