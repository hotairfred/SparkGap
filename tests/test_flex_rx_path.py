"""flex_rxant / flex_loopa / flex_loopb reach our pan and slice, and nothing is sent without them."""

import pytest

from flex_iq import FlexIQReceiver
from tests.flex_fake_radio import FakeRadio, use_fake_radio

Monkey = pytest.MonkeyPatch


def _start(monkeypatch: Monkey, refused: str = "", **kw: object) -> tuple[FakeRadio, list[str]]:
    """Start a lone copy on 20m against a fake radio; a command starting with `refused` is refused."""
    radio = use_fake_radio(monkeypatch)
    if refused:
        radio.refuse[refused] = "50000016"
    rx = FlexIQReceiver("192.0.2.1", freq_hz=14_246_000, sample_rate=192_000, daxiq_channel=3, **kw)  # pyright: ignore[reportArgumentType]
    if refused:
        with pytest.raises(RuntimeError, match="refused"):
            rx.start()
        assert rx.state != "live"
    else:
        rx.start()
        assert rx.state == "live"
    if rx._own_station:
        rx._own_station.stop()
    return radio, [c for _, c in radio.log]


def test_loop_settings_go_with_the_pan_tuning_and_onto_the_slice(monkeypatch: Monkey) -> None:
    _, sent = _start(monkeypatch, rxant="ANT1", loopb=True)
    assert "display pan set 0x40000000 center=14.246000 bandwidth=0.192000 rxant=ANT1 loopb=1" in sent
    assert "slice set 0 rxant=ANT1 loopb=1" in sent
    # the loop is set in the same command that tunes the pan, before the slice is touched
    assert sent.index("slice set 0 rxant=ANT1 loopb=1") > sent.index("slice tune 0 14.246000")


def test_loop_off_is_sent_as_zero(monkeypatch: Monkey) -> None:
    _, sent = _start(monkeypatch, loopa=False, loopb=True)
    assert "slice set 0 loopa=0 loopb=1" in sent


def test_without_options_the_radio_keeps_its_band_settings(monkeypatch: Monkey) -> None:
    _, sent = _start(monkeypatch)
    assert "display pan set 0x40000000 center=14.246000 bandwidth=0.192000" in sent
    assert not any("loop" in c or "rxant" in c for c in sent)


def test_a_loop_the_slice_refuses_fails_the_start(monkeypatch: Monkey) -> None:
    # a radio without the loop takes the pan command but not the slice's
    radio, sent = _start(monkeypatch, refused="slice set 0 loopa=", loopa=True)
    assert not any(c.startswith("stream create") for c in sent)
    assert not radio.pans and not radio.slices


def test_a_loop_the_pan_refuses_fails_the_start(monkeypatch: Monkey) -> None:
    radio, sent = _start(monkeypatch, refused="display pan set 0x40000000 center=", loopa=True)
    assert not any(c.startswith("slice tune") or c.startswith("stream create") for c in sent)
    assert not radio.pans
