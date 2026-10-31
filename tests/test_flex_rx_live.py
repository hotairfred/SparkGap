"""Our pan's receive path (antenna, loops, RF gain) read from the radio, changed live, and restored
from what the radio last reported."""

import threading

import pytest

import flex_iq
from flex_iq import FlexIQReceiver, pan_center, pan_rx, radio_loops, rfgain_steps, slice_letter
from tests.flex_fake_radio import FakeConnection, FakeRadio, use_fake_radio

Monkey = pytest.MonkeyPatch
PAN = 0x4000_0000


def _live(
    monkeypatch: Monkey, loops: bool = True, **kw: object
) -> tuple[FakeRadio, FlexIQReceiver]:
    radio = use_fake_radio(monkeypatch, loops=loops, model="FLEX-6700" if loops else "FLEX-6400M")
    rx = FlexIQReceiver("192.0.2.1", freq_hz=14_044_000, sample_rate=48_000, daxiq_channel=4, **kw)  # pyright: ignore[reportArgumentType]
    rx.start()
    assert rx.state == "live"
    return radio, rx


def _sent(radio: FakeRadio, since: int) -> list[str]:
    return [c for _, c in radio.log[since:]]


def test_pan_rx_reads_only_our_pan() -> None:
    lines = [
        "S0|display pan 0x40000001 rxant=ANT2 loopa=1",
        "S0|display pan 0x40000000 rxant=RX_A loopb=1 rfgain=16 ant_list=ANT1,ANT2,RX_A",
    ]
    assert pan_rx(lines, PAN) == {
        "rxant": "RX_A",
        "loopb": True,
        "rfgain": 16,
        "ant_list": ["ANT1", "ANT2", "RX_A"],
    }


def test_slice_letter_reads_only_our_slice() -> None:
    # 2026-10-28 FLEX-6600: SparkGap's slice 1 is B; the operator's slice 2 is A on its own client
    lines = [
        "S29C9D542|slice 1 in_use=1 client_handle=0x65C5BDA1 index_letter=B rxant=RX_B",
        "S29C9D542|slice 2 in_use=1 client_handle=0x45166B5D index_letter=A rxant=RX_B",
        "S29C9D542|slice 12 in_use=1 index_letter=C",
    ]
    assert slice_letter(lines, 1) == "B"
    assert slice_letter(lines, 2) == "A"
    assert slice_letter(lines, 0) is None
    assert slice_letter(["S0|slice 1 rxant=ANT1"], 1) is None


def test_rfgain_steps_from_the_reply() -> None:
    assert rfgain_steps("R12|0|-8,32,8") == (-8, 32, 8)
    assert rfgain_steps("R12|0|0,30,10,10,20") == (0, 30, 10)
    assert rfgain_steps("R12|50000005|") is None
    assert rfgain_steps("R12|0|0,30,0") is None
    assert rfgain_steps(None) is None


def test_start_reads_the_receive_path_and_reports_it(monkeypatch: Monkey) -> None:
    _, rx = _live(monkeypatch)
    assert rx.rx["rxant"] == "ANT1" and rx.rx["loopa"] is False and rx.rx["rfgain"] == 0
    assert rx.rfgain_range == (-8, 32, 8)
    h = rx.health()["rx"]
    assert h["ant_list"] == ["ANT1", "ANT2", "RX_A", "XVTA"] and h["rfgain_range"] == [-8, 32, 8]


def test_config_rfgain_goes_with_the_pan_tuning(monkeypatch: Monkey) -> None:
    radio, rx = _live(monkeypatch, rfgain=16)
    assert any(
        c.startswith("display pan set 0x40000000 center=") and c.endswith("rfgain=16")
        for c in _sent(radio, 0)
    )
    assert rx.rx["rfgain"] == 16


def test_set_rx_changes_pan_and_slice_and_reports_the_radio(monkeypatch: Monkey) -> None:
    radio, rx = _live(monkeypatch)
    n = len(radio.log)
    text = rx.set_rx({"rxant": "ANT2", "loopa": True, "rfgain": 24})
    sent = _sent(radio, n)
    assert "display pan set 0x40000000 rxant=ANT2 loopa=1 rfgain=24" in sent
    assert "slice set 0 rxant=ANT2 loopa=1" in sent  # no rfgain on the slice
    assert rx.rx["rxant"] == "ANT2" and rx.rx["loopa"] is True and rx.rx["rfgain"] == 24
    assert "rxant=ANT2" in text and "rxant=ANT2" in rx.rx_path


@pytest.mark.parametrize(
    "change, why",
    [
        ({"rxant": "ANT9"}, "not one this pan offers"),
        ({"loopa": 1}, "true or false"),
        ({"rfgain": 12}, "steps of 8"),
        ({"rfgain": 40}, "-8 to 32"),
        ({"rfgain": True}, "steps of 8"),
        ({"center": 14.1}, "cannot be changed live"),
        ({}, "nothing to change"),
    ],
)
def test_set_rx_refuses_what_the_radio_does_not_offer(
    monkeypatch: Monkey, change: dict[str, object], why: str
) -> None:
    radio, rx = _live(monkeypatch)
    n = len(radio.log)
    with pytest.raises(ValueError, match=why):
        rx.set_rx(change)
    assert _sent(radio, n) == []


def test_a_refusal_puts_back_what_the_radio_had(monkeypatch: Monkey) -> None:
    radio, rx = _live(monkeypatch)
    radio.refuse["slice set 0 rxant=ANT2"] = "50000016"
    n = len(radio.log)
    with pytest.raises(RuntimeError, match="refused rxant=ANT2"):
        rx.set_rx({"rxant": "ANT2"})
    sent = _sent(radio, n)
    assert sent[-2:] == [
        "display pan set 0x40000000 rxant=ANT1",
        "slice set 0 rxant=ANT1",
    ]  # put back
    assert rx.rx["rxant"] == "ANT1" and "rxant=ANT1" in rx.rx_path


def test_another_clients_change_is_shown_and_a_reconnect_restores_ours(monkeypatch: Monkey) -> None:
    radio, rx = _live(monkeypatch, rxant="ANT1", loopb=True)
    conn = rx.conn
    assert isinstance(conn, FakeConnection) and radio.pans[PAN].loopb
    radio.pans[PAN].loopb = False
    conn.queue.append("S0|display pan 0x40000000 rxant=RX_A loopb=0")  # changed in SmartSDR
    rx._poll()
    assert rx.rx["rxant"] == "RX_A" and rx.rx["loopb"] is False
    assert rx.rx_path == " rxant=ANT1 loopa=0 loopb=1"
    conn.cut()
    rx._recover("lost", "link")
    assert rx.state == "live" and rx._pan_id is not None and radio.pans[rx._pan_id].loopb


def test_loops_follow_the_model() -> None:
    assert radio_loops(['R1|0|model="FLEX-6700",name="Shack"']) == ("loopa", "loopb")
    assert radio_loops(['R1|0|model="FLEX-6500",name="Shack"']) == ("loopa",)
    assert radio_loops(['R1|0|model="FLEX-6600",name="Shack"']) == ()
    assert radio_loops([]) == ()


def test_a_model_without_loops_offers_none_though_its_status_lists_them(
    monkeypatch: Monkey, caplog: pytest.LogCaptureFixture
) -> None:
    radio = use_fake_radio(monkeypatch, loops=True, model="FLEX-6600")
    rx = FlexIQReceiver(
        "192.0.2.1", freq_hz=14_044_000, sample_rate=48_000, daxiq_channel=4, loopa=True
    )
    rx.start()
    assert "loopa" not in rx.rx and rx.health()["rx"]["loopb"] is None
    assert not any("loopa" in c for _, c in radio.log)  # the config's loop is never sent
    assert "flex_loopa set, but this radio model has no such loop" in caplog.text
    with pytest.raises(ValueError, match="has no loopa"):
        rx.set_rx({"loopa": True})


def test_a_loop_the_radio_does_not_report_is_not_offered(monkeypatch: Monkey) -> None:
    radio, rx = _live(monkeypatch, loops=False)  # a 6400M: no loops in its pan status
    assert "loopa" not in rx.rx and rx.health()["rx"]["loopa"] is None
    n = len(radio.log)
    with pytest.raises(ValueError, match="has no loopa"):
        rx.set_rx({"loopa": True})
    assert _sent(radio, n) == []


def test_not_live_changes_nothing(monkeypatch: Monkey) -> None:
    radio = use_fake_radio(monkeypatch)
    rx = FlexIQReceiver("192.0.2.1", freq_hz=14_044_000, sample_rate=48_000, daxiq_channel=4)
    with pytest.raises(RuntimeError, match="Not live"):
        rx.set_rx({"loopa": True})
    assert radio.log == []


def test_a_live_change_during_a_recovery_is_refused_at_once(monkeypatch: Monkey) -> None:
    radio, rx = _live(monkeypatch)
    monkeypatch.setattr(flex_iq, "LIVE_LOCK_WAIT_S", 0.05)
    held, done = threading.Event(), threading.Event()

    def recovery() -> None:  # an outage holds the receiver's lock
        with rx._lock:
            held.set()
            done.wait(5)

    t = threading.Thread(target=recovery)
    t.start()
    try:
        held.wait(2)
        n = len(radio.log)
        with pytest.raises(flex_iq.Recovering, match="^recovering"):
            rx.set_rx({"rfgain": 8})
        with pytest.raises(flex_iq.Recovering):
            rx.retune(7_040_000)
        assert [c for c in _sent(radio, n) if c != "ping"] == []  # the heartbeat goes on
    finally:
        done.set()
        t.join()


def test_after_a_change_the_target_is_what_the_radio_reported(monkeypatch: Monkey) -> None:
    radio, rx = _live(monkeypatch)
    radio.pans[PAN].rfgain = 8
    orig = radio.run

    def run(handle: int, cmd: str) -> tuple[str, str, list[str]]:
        code, text, status = orig(handle, cmd)
        if cmd.startswith("display pan set") and "rfgain=24" in cmd:
            radio.pans[PAN].rfgain = 8  # another pan on the SCU set it back meanwhile
            status = [radio.pan_line(PAN)]
        return code, text, status

    monkeypatch.setattr(radio, "run", run)
    assert rx.set_rx({"rfgain": 24}) == "receive path rfgain=8"
    assert rx.rx["rfgain"] == 8 and rx._target["rfgain"] == 8


def test_pan_center_reads_only_our_pan_and_the_last_report() -> None:
    lines = [
        "S0|display pan 0x40000001 center=7.040000 rxant=ANT1",
        "S0|display pan 0x40000000 center=14.044000 rxant=ANT1",
        "S0|display pan 0x40000000 rxant=ANT2",
        "S0|display pan 0x40000000 center=14.055000",
    ]
    assert pan_center(lines, PAN) == 14.055
    assert pan_center(lines[:1], PAN) is None


def test_our_pan_at_our_centre_stays_live(monkeypatch: Monkey) -> None:
    radio, rx = _live(monkeypatch)
    conn = rx.conn
    assert isinstance(conn, FakeConnection)
    conn.queue.append(radio.pan_line(PAN))
    rx._poll()
    assert rx.state == "live"


def test_a_pan_moved_by_another_client_halts(monkeypatch: Monkey) -> None:
    radio, rx = _live(monkeypatch)
    conn = rx.conn
    assert isinstance(conn, FakeConnection)
    radio.pans[PAN].center = 14.055
    conn.queue.append(radio.pan_line(PAN))
    rx._poll()
    assert rx.state == "halted" and not radio.pans


def test_our_own_retune_does_not_trip_the_guard(monkeypatch: Monkey) -> None:
    radio, rx = _live(monkeypatch)
    rx.retune(7_040_000)
    conn = rx.conn
    assert isinstance(conn, FakeConnection) and rx._pan_id is not None
    conn.queue.append(radio.pan_line(rx._pan_id))  # the radio's report of our move, read late
    rx._poll()
    assert rx.state == "live" and radio.pans[rx._pan_id].center == pytest.approx(7.04)


def test_a_move_right_after_our_retune_still_halts(monkeypatch: Monkey) -> None:
    radio, rx = _live(monkeypatch)
    rx.retune(7_040_000)
    conn = rx.conn
    assert isinstance(conn, FakeConnection) and rx._pan_id is not None
    radio.pans[rx._pan_id].center = 7.040002  # 2 Hz, inside the old settle window
    conn.queue.append(radio.pan_line(rx._pan_id))
    rx._poll()
    assert rx.state == "halted"
