"""flex_rxant / flex_loopa / flex_loopb reach our pan and slice, and nothing is sent without them."""

import pytest

import flex_iq
from flex_iq import FlexIQReceiver

Monkey = pytest.MonkeyPatch
HANDLE = "7F19D8AE"
DEFAULT_PAN = [f"S{HANDLE}|display pan 0x40000001 client_handle=0x{HANDLE} center=14.100000 "
               "rxant=ANT1 loopa=0 loopb=0 daxiq_channel=0",
               f"S{HANDLE}|slice 0 in_use=1 RF_frequency=14.100000 client_handle=0x{HANDLE} pan=0x40000001"]


class _Sock:
    def settimeout(self, *_: object) -> None:
        pass

    def connect(self, *_: object) -> None:
        pass

    def setsockopt(self, *_: object) -> None:
        pass

    def bind(self, *_: object) -> None:
        pass


def _setup(monkeypatch: Monkey, **kw: object) -> list[str]:
    """Run _setup against a radio that gives us its default pan and slice; returns the commands."""
    rx = FlexIQReceiver("192.0.2.1", freq_hz=14_246_000, sample_rate=192_000, daxiq_channel=3, **kw)
    sent: list[str] = []

    def cmd(c: str, timeout: float = 2.0, settle: float = 0.0) -> list[str]:
        rx._seq += 1
        sent.append(c)
        if c == "info":
            return [f'R{rx._seq}|0|model="FLEX-6700",name="Shack"']
        if c in ("sub pan all", "sub slice all"):
            return DEFAULT_PAN + [f"R{rx._seq}|0|"]
        if c.startswith("stream create"):
            return [f"R{rx._seq}|0|0x20000004"]
        return [f"R{rx._seq}|0|"]

    monkeypatch.setattr(flex_iq.socket, "socket", lambda *_: _Sock())
    monkeypatch.setattr(rx, "_drain", lambda *_: ["V1.4.0.0", f"H{HANDLE}"])
    monkeypatch.setattr(rx, "_cmd", cmd)
    rx._setup()
    assert rx.state == "live" and rx._pan_id == 0x4000_0001 and rx._slice_id == 0
    return sent


def test_loop_settings_go_with_the_pan_tuning_and_onto_the_slice(monkeypatch: Monkey) -> None:
    sent = _setup(monkeypatch, rxant="ANT1", loopb=True)
    assert "display pan set 0x40000001 center=14.246000 bandwidth=0.192000 rxant=ANT1 loopb=1" in sent
    assert "slice set 0 rxant=ANT1 loopb=1" in sent
    # the loop is set in the same command that tunes the pan, before the slice is touched
    assert sent.index("slice set 0 rxant=ANT1 loopb=1") > sent.index("slice tune 0 14.246000")


def test_loop_off_is_sent_as_zero(monkeypatch: Monkey) -> None:
    sent = _setup(monkeypatch, loopa=False, loopb=True)
    assert "slice set 0 loopa=0 loopb=1" in sent


def test_without_options_the_radio_keeps_its_band_settings(monkeypatch: Monkey) -> None:
    sent = _setup(monkeypatch)
    assert "display pan set 0x40000001 center=14.246000 bandwidth=0.192000" in sent
    assert not any("loop" in c or "rxant" in c for c in sent)
