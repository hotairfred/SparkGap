"""One SparkGap station per radio, each band's channel bound to it: replays of the 2026-10-21/22
Dallas failures against a fake radio, plus recovery, the loss budget and retune."""

import logging
import struct

import pytest

import flex_iq
from flex_iq import FlexIQReceiver, FlexStation, LossBudget, StationExists
from tests.flex_fake_radio import (
    FakeConnection,
    FakeRadio,
    Pan,
    Slice,
    Stream,
    _Udp,
    use_fake_radio,
)

Monkey = pytest.MonkeyPatch
Caplog = pytest.LogCaptureFixture
MHZ_17M, MHZ_20M, MHZ_40M = 18.1064, 14.0384, 7.0384


@pytest.fixture
def radio(monkeypatch: Monkey) -> FakeRadio:
    return use_fake_radio(monkeypatch)


def _station(radio: FakeRadio) -> FlexStation:
    st = FlexStation("192.0.2.1")
    st.start()
    return st


def _channel(mhz: float, channel: int) -> FlexIQReceiver:
    rx = FlexIQReceiver(
        "192.0.2.1",
        freq_hz=round(mhz * 1e6),
        sample_rate=96_000,
        daxiq_channel=channel,
        udp_port=7790 + channel,
    )
    rx.start()
    return rx


def _pan_of(radio: FakeRadio, rx: FlexIQReceiver) -> float:
    assert rx._pan_id is not None
    return radio.pans[rx._pan_id].center


def _slice_of(radio: FakeRadio, rx: FlexIQReceiver) -> Slice:
    assert rx._slice_id is not None
    return radio.slices[rx._slice_id]


def _cut(conn: object) -> None:
    assert isinstance(conn, FakeConnection)
    conn.cut()


def _gui_commands(radio: FakeRadio) -> list[str]:
    return [c for _, c in radio.log if c.startswith("client gui")]


def test_the_station_holds_the_gui_client_with_persistence_off_and_no_pan(radio: FakeRadio) -> None:
    st = _station(radio)
    cmds = [c for _, c in radio.log]
    assert cmds.index("client start_persistence off") < cmds.index(f"client gui {st.client_id}")
    assert "keepalive enable" in cmds
    assert st.station == "SparkGap-Dallas" and st.state == "live"
    assert not radio.pans and not radio.slices  # the default pan and slice are removed


def test_a_second_station_process_refuses_to_evict_the_first(radio: FakeRadio) -> None:
    _station(radio)
    with pytest.raises(StationExists):
        _station(radio)
    assert len(_gui_commands(radio)) == 1


def test_two_channels_bind_and_each_owns_its_own_pan_and_slice(radio: FakeRadio) -> None:
    _station(radio)
    a, b = _channel(MHZ_17M, 1), _channel(MHZ_20M, 2)
    assert len(_gui_commands(radio)) == 1
    assert _pan_of(radio, a) == MHZ_17M and _pan_of(radio, b) == MHZ_20M
    assert a._slice_id != b._slice_id
    assert _slice_of(radio, a).pan == a._pan_id and _slice_of(radio, b).pan == b._pan_id


def test_a_band_change_on_one_channel_leaves_the_other_alone(radio: FakeRadio) -> None:
    """2026-10-22 11:50Z: the restart took the other copy's slice and moved its pan to 40m."""
    _station(radio)
    a, b = _channel(MHZ_17M, 1), _channel(MHZ_20M, 2)
    a_objects = (a._pan_id, a._slice_id, a._stream_id)
    b.stop()
    b2 = _channel(MHZ_40M, 2)
    assert (a._pan_id, a._slice_id, a._stream_id) == a_objects
    assert _pan_of(radio, a) == MHZ_17M and _slice_of(radio, a).freq == MHZ_17M
    assert _pan_of(radio, b2) == MHZ_40M and _slice_of(radio, b2).pan == b2._pan_id


def test_stop_removes_only_this_channels_objects(radio: FakeRadio) -> None:
    _station(radio)
    a, b = _channel(MHZ_17M, 1), _channel(MHZ_20M, 2)
    b_pan, b_slice = b._pan_id, b._slice_id
    a.stop()
    assert set(radio.pans) == {b_pan} and set(radio.slices) == {b_slice}
    assert [st.channel for st in radio.streams.values()] == [2]


def _kill(radio: FakeRadio, rx: FlexIQReceiver) -> None:
    """kill -9: the copy's control connection drops and no cleanup runs."""
    assert rx.conn is not None
    for conn in [c for c in radio.conns if c.handle == rx.conn.handle]:
        conn.lost = "killed"
        radio.conns.remove(conn)
    radio.drop(int(rx.conn.handle, 16))


def test_a_killed_copy_restarted_on_its_channel_leaves_no_pan_behind(radio: FakeRadio) -> None:
    st = _station(radio)
    _kill(radio, _channel(MHZ_17M, 1))
    assert len(radio.pans) == 1 and not radio.streams       # the station kept the dead copy's pan
    a = _channel(MHZ_17M, 1)
    assert set(radio.pans) == {a._pan_id} and set(radio.slices) == {a._slice_id}
    a.stop()
    assert not radio.pans and not radio.slices
    st.stop()


def test_a_killed_copy_restarts_on_a_two_pan_radio_leaving_the_other(radio: FakeRadio) -> None:
    _station(radio)                                          # max_pans 2, as a FLEX-6400
    _kill(radio, _channel(MHZ_17M, 1))
    b = _channel(MHZ_20M, 2)
    b_objects = (b._pan_id, b._slice_id, b._stream_id)
    a = _channel(MHZ_17M, 1)
    assert a.state == "live" and _pan_of(radio, a) == MHZ_17M
    assert (b._pan_id, b._slice_id, b._stream_id) == b_objects and b._stream_id in radio.streams


def test_a_pan_another_copy_takes_over_meanwhile_is_left_alone(
    radio: FakeRadio, monkeypatch: Monkey
) -> None:
    radio.max_pans = 4
    st = _station(radio)
    _kill(radio, _channel(MHZ_17M, 1))
    (orphan,) = radio.pans
    assert st.conn is not None
    gui = int(st.conn.handle, 16)
    run = radio.run
    taken: list[Pan] = []

    def another_copy_starts(handle: int, cmd: str):  # after our first status, before we act
        if cmd == "sub tx all" and orphan in radio.pans and not radio.streams:
            radio.remove_pan(orphan)
            taken.append(Pan(gui, MHZ_17M, 1))
            radio.pans[orphan] = taken[0]  # the same id, now another copy's live pan
            radio.streams[0x2000_0F00] = Stream(1, 0x7777_0001, gui, orphan)
        return run(handle, cmd)

    monkeypatch.setattr(radio, "run", another_copy_starts)
    _channel(MHZ_17M, 1)
    assert taken and radio.pans.get(orphan) is taken[0]


def test_another_channels_leftover_pan_is_not_ours_to_remove(radio: FakeRadio) -> None:
    radio.max_pans = 4
    _station(radio)
    _kill(radio, _channel(MHZ_20M, 2))
    left = set(radio.pans)
    a = _channel(MHZ_17M, 1)
    assert set(radio.pans) == left | {a._pan_id}


def test_a_lone_copy_holds_the_station_and_the_next_copy_binds_to_it(radio: FakeRadio) -> None:
    a = _channel(MHZ_17M, 1)
    assert a._own_station is not None
    b = _channel(MHZ_20M, 2)
    assert b._own_station is None and len(_gui_commands(radio)) == 1
    assert _pan_of(radio, a) == MHZ_17M and _pan_of(radio, b) == MHZ_20M
    a._own_station.stop()


def test_both_channels_reconnecting_at_once_only_bind(radio: FakeRadio) -> None:
    """2026-10-21 23:19Z: both copies sent client gui with the same id; one halted."""
    _station(radio)
    a, b = _channel(MHZ_17M, 1), _channel(MHZ_20M, 2)
    for rx in (a, b):
        _cut(rx.conn)
    a._recover("lost", "link")
    b._recover("lost", "link")
    assert len(_gui_commands(radio)) == 1
    assert a.state == b.state == "live"
    assert _pan_of(radio, a) == MHZ_17M and _pan_of(radio, b) == MHZ_20M
    assert (
        len(radio.pans) == 2 and len(radio.streams) == 2
    )  # the lost connections' leftovers removed


def test_a_stalled_stream_gets_a_new_stream_and_keeps_its_pan_and_slice(radio: FakeRadio) -> None:
    _station(radio)
    a, b = _channel(MHZ_17M, 1), _channel(MHZ_20M, 2)
    pan, sl, old = a._pan_id, a._slice_id, a._stream_id
    n_cmds = len(radio.log)
    a._recover("No DAX-IQ packets for 10 s", "stream")
    sent = [c for _, c in radio.log[n_cmds:]]
    assert sent.index(f"stream remove 0x{old:08x}") < sent.index(
        "stream create type=dax_iq daxiq_channel=1"
    )
    assert (a._pan_id, a._slice_id) == (pan, sl) and a.state == "live"
    assert not any(c.startswith(("client ", "display pan create", "slice ")) for c in sent)
    assert (
        a._stream_id is not None
        and radio.streams[a._stream_id].pan == pan
        and _pan_of(radio, b) == MHZ_20M
    )


def test_a_stream_the_radio_refuses_to_remove_halts_instead_of_doubling_up(
    radio: FakeRadio,
) -> None:
    _station(radio)
    a = _channel(MHZ_17M, 1)
    radio.refuse["stream remove"] = "50000005"  # and it stays listed
    a._recover("No DAX-IQ packets for 10 s", "stream")
    assert a.state == "halted"
    assert not any(c == "stream create type=dax_iq daxiq_channel=1" for _, c in radio.log[-6:])


def test_a_channel_rebuilds_when_its_station_registers_again(radio: FakeRadio) -> None:
    st = _station(radio)
    a = _channel(MHZ_17M, 1)
    _cut(st.conn)  # the radio drops the station and every pan of it
    assert not radio.pans
    st.start(again=True)
    a._poll()
    assert a._station_lost
    a._recover("Station left the radio", "station")
    assert a.state == "live" and _pan_of(radio, a) == MHZ_17M
    assert len(_gui_commands(radio)) == 2 and all(
        rx == "client gui " + st.client_id for rx in _gui_commands(radio)
    )


def test_a_channel_whose_station_never_returns_holds_it_and_builds_again(radio: FakeRadio) -> None:
    st = _station(radio)
    a = _channel(MHZ_17M, 1)
    _cut(st.conn)  # flex_station.py crashed
    a._recover("Station left the radio", "station")
    assert a.state == "live" and a._own_station is not None and _pan_of(radio, a) == MHZ_17M
    assert len(_gui_commands(radio)) == 2
    a.stop()


def test_stopping_the_lone_copy_that_holds_the_station_leaves_the_next_copy_running(
    radio: FakeRadio,
) -> None:
    """hotairfred/SparkGap#94 review: stopping the copy holding the station took the others down."""
    a = _channel(MHZ_17M, 1)
    b = _channel(MHZ_20M, 2)
    assert a._own_station is not None and b._own_station is None
    a.stop()
    assert not radio.pans  # the radio removes every pan of the station
    b._poll()
    assert b._station_lost
    b._recover("Station left the radio", "station")
    assert b.state == "live" and b._own_station is not None
    assert _pan_of(radio, b) == MHZ_20M and len(radio.pans) == 1 and len(radio.streams) == 1
    c = _channel(MHZ_17M, 1)  # the stopped copy started again binds to it
    assert c._own_station is None and len(_gui_commands(radio)) == 2
    c.stop()
    b.stop()


def test_after_a_station_crash_one_copy_holds_it_and_the_other_binds(
    radio: FakeRadio, monkeypatch: Monkey, caplog: Caplog
) -> None:
    """Copies wait STATION_TAKEOVER_S x their channel: channel 1 registers while channel 2 waits."""
    st = _station(radio)
    a, b = _channel(MHZ_17M, 1), _channel(MHZ_20M, 2)
    _cut(st.conn)
    monkeypatch.setattr(flex_iq, "STATION_TAKEOVER_S", 10)
    run, polls = radio.run, []

    def a_first(handle: int, cmd: str) -> tuple[str, str, list[str]]:
        if cmd == "sub client all" and b.conn and handle == int(b.conn.handle, 16):
            polls.append(cmd)
            if len(polls) == 2:  # b's first poll while it waits: channel 1's wait ends first
                monkeypatch.setattr(flex_iq, "STATION_TAKEOVER_S", 0.01)
                a._recover("Station left the radio", "station")
                monkeypatch.setattr(flex_iq, "STATION_TAKEOVER_S", 10)
        return run(handle, cmd)

    monkeypatch.setattr(radio, "run", a_first)
    with caplog.at_level(logging.INFO, logger="sparkgap.flex"):
        b._recover("Station left the radio", "station")
    assert "Waiting up to 20 s for station" in caplog.text
    assert caplog.text.count("this copy holds it") == 1  # a's; b only attached
    assert a.state == b.state == "live" and a._own_station is not None and b._own_station is None
    assert len(_gui_commands(radio)) == 2
    assert _pan_of(radio, a) == MHZ_17M and _pan_of(radio, b) == MHZ_20M
    b.stop()
    a.stop()


def test_the_station_process_leaves_a_station_a_copy_took_over(radio: FakeRadio) -> None:
    st = _station(radio)
    a = _channel(MHZ_17M, 1)
    _cut(st.conn)
    a._recover("Station left the radio", "station")
    assert not st._recover("lost")
    assert st.state == "halted" and len(_gui_commands(radio)) == 2
    assert a.state == "live" and _pan_of(radio, a) == MHZ_17M
    a.stop()


def test_the_station_registers_over_its_own_stale_entry(
    radio: FakeRadio, monkeypatch: Monkey
) -> None:
    st = _station(radio)
    a = _channel(MHZ_17M, 1)
    old = st.conn
    assert isinstance(old, FakeConnection)
    old.lost = "No ping reply for 15 s"
    monkeypatch.setattr(old, "close", lambda _why: radio.conns.remove(old))  # still listed
    assert st._recover("No ping reply for 15 s")
    assert int(old.handle, 16) not in radio.clients  # evicted by our own registration
    assert st.state == "live" and len(_gui_commands(radio)) == 2
    a._poll()
    a._recover("Station left the radio", "station")
    assert a._own_station is None and a.state == "live"


def test_three_losses_an_hour_are_recovered_and_the_fourth_stops(radio: FakeRadio) -> None:
    _station(radio)
    a = _channel(MHZ_17M, 1)
    for _ in range(3):
        a._recover("No DAX-IQ packets for 10 s", "stream")
        assert a.state == "live"
    a._recover("No DAX-IQ packets for 10 s", "stream")
    assert a.state == "halted" and not radio.pans and not radio.streams


def test_the_loss_budget_rolls_over_the_hour() -> None:
    b = LossBudget(budget=3, window=3600)
    assert all(b.take("x", now=t) for t in (0, 10, 20))
    assert not b.take("x", now=30)
    assert b.take("x", now=3601)


def test_unanswered_pings_are_ridden_out_while_the_connection_lasts(radio: FakeRadio) -> None:
    """2026-10-22 14:02Z: the station left 11 s into a 17 s outage, taking every pan with it."""
    st = _station(radio)
    radio.down = 5
    assert all(st._beat() is None for _ in range(5))
    assert st._beat() is None and st._unanswered is None and len(_gui_commands(radio)) == 1


def test_the_station_registers_again_once_the_radio_dropped_it(radio: FakeRadio) -> None:
    st = _station(radio)
    radio.drop_all()  # the outage outlasted the radio's 15 s keepalive
    why = st._beat()
    assert why
    radio.down = 3  # still unreachable for a while
    assert st._recover(why) and st.state == "live" and len(_gui_commands(radio)) == 2


def test_the_station_stops_when_the_radio_never_comes_back(
    monkeypatch: Monkey, radio: FakeRadio
) -> None:
    st = _station(radio)
    monkeypatch.setattr(flex_iq, "OUTAGE_MAX_S", 0.01)
    radio.drop_all()
    radio.down = 10**6
    assert not st._recover("The radio closed the control connection") and st.state == "halted"


def test_the_station_stops_when_its_budget_is_spent(radio: FakeRadio) -> None:
    st = _station(radio)
    st.budget = LossBudget(budget=0)
    assert not st._recover("The radio closed the control connection") and st.state == "halted"


def test_a_stall_during_a_short_outage_keeps_its_pan_and_slice(radio: FakeRadio) -> None:
    """The 2026-10-22 14:02Z pull, replayed: replies stop for a while, then the radio answers."""
    _station(radio)
    a, b = _channel(MHZ_17M, 1), _channel(MHZ_20M, 2)
    pan, sl = a._pan_id, a._slice_id
    radio.down = 4
    a._recover("No DAX-IQ packets for 10 s", "stream")
    assert a.state == "live" and (a._pan_id, a._slice_id) == (pan, sl)
    assert sorted(st.channel for st in radio.streams.values()) == [1, 2]
    assert _pan_of(radio, b) == MHZ_20M and len(_gui_commands(radio)) == 1


def test_a_stream_that_resumes_by_itself_is_kept(radio: FakeRadio) -> None:
    _station(radio)
    a = _channel(MHZ_17M, 1)
    assert a._stream_id is not None
    head = struct.pack(">BBHI", 0x38, 0x01, 0, a._stream_id) + bytes(flex_iq.VITA_HEADER_SIZE - 8)
    _Udp.packets = [head + bytes(32)]
    n = len(radio.log)
    a._recover("No DAX-IQ packets for 10 s", "stream")
    assert a.state == "live" and not any(c.startswith("stream ") for _, c in radio.log[n:])


def test_no_reply_to_the_stream_status_never_makes_a_second_stream(radio: FakeRadio) -> None:
    _station(radio)
    a = _channel(MHZ_17M, 1)
    radio.silent.add("sub daxiq")  # no reply: not ready, so it rebuilds on a new connection
    a._recover("No DAX-IQ packets for 10 s", "stream")
    assert a.state == "live" and [st.channel for st in radio.streams.values()] == [1]
    assert a.conn is not None and a._stream_id in radio.streams
    assert radio.streams[a._stream_id].client == int(a.conn.handle, 16)


def test_a_router_reset_is_recovered_by_the_station_then_its_copies(radio: FakeRadio) -> None:
    st = _station(radio)
    a, b = _channel(MHZ_17M, 1), _channel(MHZ_20M, 2)
    radio.drop_all()  # minutes without a network: the radio dropped everyone
    radio.down = 6
    assert st._recover(st._beat() or "lost")
    for rx in (a, b):
        assert rx._poll() is None and rx.conn is not None
        rx._recover(rx.conn.lost or "lost", "link")
    assert a.state == b.state == "live" and len(_gui_commands(radio)) == 2
    assert _pan_of(radio, a) == MHZ_17M and _pan_of(radio, b) == MHZ_20M


def test_retune_moves_only_our_pan_and_slice(radio: FakeRadio) -> None:
    _station(radio)
    a, b = _channel(MHZ_17M, 1), _channel(MHZ_20M, 2)
    n_cmds = len(radio.log)
    a.retune(round(MHZ_40M * 1e6))
    sent = [c for _, c in radio.log[n_cmds:]]
    assert sent == [
        f"slice unlock {a._slice_id}",
        f"display pan set 0x{a._pan_id:08x} center={MHZ_40M:.6f} bandwidth=0.096000",
        f"slice tune {a._slice_id} {MHZ_40M:.6f}",
        f"slice lock {a._slice_id}",
    ]
    assert _slice_of(radio, a).locked and _slice_of(radio, b).locked
    assert _pan_of(radio, a) == MHZ_40M and _pan_of(radio, b) == MHZ_20M
    assert a.frequencies == [round(MHZ_40M * 1e6)] and a._discard_until > flex_iq.time.time()


def test_a_refused_retune_puts_the_pan_back_and_raises(radio: FakeRadio) -> None:
    _station(radio)
    a = _channel(MHZ_17M, 1)
    radio.refuse["slice tune"] = "50000005"
    with pytest.raises(RuntimeError, match="refused 7.038400 MHz$"):
        a.retune(round(MHZ_40M * 1e6))
    assert _pan_of(radio, a) == MHZ_17M and a.freq_mhz == MHZ_17M


def test_a_channel_smartsdr_holds_is_refused_by_name(radio: FakeRadio) -> None:
    smartsdr = radio.connect()
    smartsdr.open()
    smartsdr.cmd("client station SUPERWIN10")
    smartsdr.cmd("client program SmartSDR-Win")
    smartsdr.cmd("client gui BBBB-2")
    pan = next(iter(radio.pans))
    radio.pans[pan].channel = 1
    _station(radio)
    with pytest.raises(RuntimeError, match="DAX-IQ 1 is in use by SUPERWIN10 .SmartSDR-Win."):
        _channel(MHZ_17M, 1)


def test_a_second_copy_on_the_same_channel_is_refused(radio: FakeRadio) -> None:
    _station(radio)
    _channel(MHZ_17M, 1)
    with pytest.raises(RuntimeError, match="DAX-IQ 1 is in use by SparkGap-Dallas"):
        _channel(MHZ_20M, 1)
    assert len(radio.pans) == 1


def test_recovery_is_logged_with_its_kind(radio: FakeRadio, caplog: Caplog) -> None:
    _station(radio)
    a = _channel(MHZ_17M, 1)
    with caplog.at_level(logging.INFO, logger="sparkgap.flex"):
        a._recover("No DAX-IQ packets for 10 s", "stream")
    assert "Recovered (stream)" in caplog.text


def test_a_reused_pan_id_is_left_alone_after_a_lost_connection(radio: FakeRadio) -> None:
    """Codex audit 2026-10-22: the old pan id went to another copy's new pan, not streaming yet."""
    st = _station(radio)
    a = _channel(MHZ_17M, 1)
    old_pan = a._pan_id
    _cut(a.conn)
    _cut(st.conn)  # the station leaves: the radio drops a's pan
    st.start(again=True)
    b = _channel(MHZ_20M, 2)
    assert b._pan_id == old_pan and b._stream_id is not None
    del radio.streams[b._stream_id]  # b between its pan and its stream
    a._recover("The radio closed the control connection", "link")
    assert old_pan in radio.pans and radio.pans[old_pan].center == MHZ_20M
    assert a.state == "live" and _pan_of(radio, a) == MHZ_17M


def test_each_channel_locks_its_slice_so_a_vfo_click_cannot_move_it(radio: FakeRadio) -> None:
    _station(radio)
    a = _channel(MHZ_17M, 1)
    assert _slice_of(radio, a).locked
    other = radio.connect()
    other.open()
    assert other.cmd(f"slice tune {a._slice_id} 18.120000")[-1].split("|")[1] != "0"
    assert _slice_of(radio, a).freq == MHZ_17M


def test_a_refused_retune_leaves_the_slice_locked(radio: FakeRadio) -> None:
    _station(radio)
    a = _channel(MHZ_17M, 1)
    radio.refuse["display pan set"] = "50000005"
    with pytest.raises(RuntimeError):
        a.retune(round(MHZ_40M * 1e6))
    assert _slice_of(radio, a).locked and _slice_of(radio, a).freq == MHZ_17M


def test_a_slice_that_comes_up_locked_is_unlocked_tuned_and_locked_again(radio: FakeRadio) -> None:
    """Dallas 2026-10-22 09:15: the band's lock came back on the new pan's slice (50000068)."""
    radio.slices_locked = True
    _station(radio)
    a = _channel(MHZ_17M, 1)
    assert a.state == "live" and _slice_of(radio, a).freq == MHZ_17M and _slice_of(radio, a).locked


def _pings(radio: FakeRadio, since: int) -> int:
    return sum(c == "ping" for _, c in radio.log[since:])


def test_after_an_outage_one_answered_ping_is_not_enough(
    monkeypatch: Monkey, radio: FakeRadio
) -> None:
    """14:19Z: the radio answered a ping, then went quiet; recovery must wait out SETTLE_S."""
    monkeypatch.setattr(flex_iq, "SETTLE_S", 0.02)
    n = len(radio.log)
    conn = flex_iq.await_radio("192.0.2.1", 4992)
    assert conn is not None and _pings(radio, n) == 1
    n = len(radio.log)
    conn = flex_iq.await_radio("192.0.2.1", 4992, conn=conn, outage=True)
    assert conn is not None and _pings(radio, n) > 1


def test_the_station_waits_again_when_registering_gets_no_reply(radio: FakeRadio) -> None:
    st = _station(radio)
    radio.drop_all()
    radio.unanswered["client gui"] = 1  # answers pings, but not ready to register yet
    assert st._recover("The radio closed the control connection") and st.state == "live"
    assert len(_gui_commands(radio)) == 2


def test_a_refused_registration_still_stops_the_station(radio: FakeRadio) -> None:
    st = _station(radio)
    radio.drop_all()
    radio.refuse["client gui"] = "50000016"
    assert not st._recover("The radio closed the control connection") and st.state == "halted"


def test_a_copy_waits_again_when_its_rebuild_gets_no_reply(radio: FakeRadio) -> None:
    st = _station(radio)
    a = _channel(MHZ_17M, 1)
    _cut(a.conn)
    radio.unanswered["stream create"] = 1
    a._recover("The radio closed the control connection", "link")
    assert a.state == "live" and _pan_of(radio, a) == MHZ_17M
    assert len(radio.pans) == 1 and len(radio.streams) == 1 and st.state == "live"


def test_a_station_stopped_while_registering_leaves_no_gui_client(radio: FakeRadio) -> None:
    st = FlexStation("192.0.2.1")
    st.request_stop()
    with pytest.raises(RuntimeError, match="stopping"):
        st.start()
    assert not any(c.gui for c in radio.clients.values())


def test_a_stall_that_finds_a_new_station_rebuilds_without_a_second_settle(
    monkeypatch: Monkey, radio: FakeRadio
) -> None:
    """14:49Z: the station registered again while the copies waited; their pans had gone with it."""
    st = _station(radio)
    a = _channel(MHZ_17M, 1)
    _cut(st.conn)  # the radio dropped the station and its pans; the copy's connection lasted
    st.start(again=True)
    settles: list[bool] = []
    real = flex_iq.await_radio

    def counting(*args: object, outage: bool = False, **kw: object) -> object:
        settles.append(outage)
        return real(*args, outage=outage, **kw)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(flex_iq, "await_radio", counting)
    a._recover("No DAX-IQ packets for 10 s", "stream")
    assert a.state == "live" and _pan_of(radio, a) == MHZ_17M and settles == [False]


def test_a_refusal_after_a_missed_ping_still_stops_the_station(radio: FakeRadio) -> None:
    st = _station(radio)
    radio.drop_all()
    radio.unanswered["ping"] = 1  # one miss while it settles
    radio.refuse["client gui"] = "50000016"
    assert not st._recover("The radio closed the control connection") and st.state == "halted"
    assert (
        sum(c.startswith("client gui") for _, c in radio.log) == 2
    )  # first start, then one refusal


def test_a_retune_the_radio_never_answers_halts_rather_than_decode_an_unknown_frequency(
    radio: FakeRadio,
) -> None:
    _station(radio)
    a = _channel(MHZ_17M, 1)
    radio.silent.add("slice tune")  # the slice may have moved, and it cannot be put back
    with pytest.raises(RuntimeError, match="halted"):
        a.retune(round(MHZ_40M * 1e6))
    assert a.state == "halted" and a._pan_id is None and a.freq_mhz == MHZ_17M


def test_a_retune_while_not_live_is_refused_and_sends_nothing(radio: FakeRadio) -> None:
    _station(radio)
    a = _channel(MHZ_17M, 1)
    a.state = "reconnecting"
    n_cmds = len(radio.log)
    with pytest.raises(RuntimeError, match="Not live"):
        a.retune(round(MHZ_40M * 1e6))
    assert len(radio.log) == n_cmds


def test_a_retune_drops_the_iq_queued_from_the_old_frequency(radio: FakeRadio) -> None:
    """Codex audit 2026-10-22: a backlog in the receive buffer can outlast the settle time."""
    from tests.flex_fake_radio import _Udp  # pyright: ignore[reportPrivateUsage]

    _station(radio)
    a = _channel(MHZ_17M, 1)
    _Udp.packets = [b"old"] * 5
    a.retune(round(MHZ_40M * 1e6))
    assert _Udp.packets == []


def test_a_copy_whose_station_was_evicted_takes_over_again_later(radio: FakeRadio) -> None:
    a = _channel(MHZ_17M, 1)
    b = _channel(MHZ_20M, 2)
    own = a._own_station
    assert own is not None and own._thread is not None
    own.request_stop()  # drive its recovery here instead of on its heartbeat thread
    own._thread.join()  # leaving, it drops its GUI client: lost
    own._stop.clear()
    assert own.conn is None and not radio.pans
    other = FlexStation("192.0.2.1")
    other.start()
    assert not own._recover("lost") and own.state == "halted"
    for rx in (a, b):
        rx._poll()
        rx._recover("Station left the radio", "station")
    assert a._own_station is None and a.state == b.state == "live"
    assert other.conn is not None
    _cut(other.conn)  # the new holder goes too: a copy takes over, not wait 600 s
    for rx in (a, b):
        rx._poll()
        rx._recover("Station left the radio", "station")
    assert a._own_station is not None and a.state == b.state == "live"
    b.stop()
    a.stop()


def test_a_station_still_registering_is_bound_to_not_registered_over(radio: FakeRadio) -> None:
    """Dallas 2026-10-27 02:37Z: a station is listed by its client ID about 6 s before its name."""
    conn = radio.connect()
    conn.open()
    conn.cmd("client program SparkGap")
    conn.cmd("client gui " + flex_iq.station_client_id("192.0.2.1", "SparkGap-Dallas"))
    a = _channel(MHZ_17M, 1)
    assert a._own_station is None and a.state == "live" and len(_gui_commands(radio)) == 1
    assert a._station_handle == int(conn.handle, 16)
    a.stop()
