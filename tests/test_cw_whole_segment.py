"""cw_whole_segment (cdub89/SparkGap#48): the CW window runs to the top of the US CW/data segment,
with the FT8 and JS8 dials inside skipped as zones instead of ending the window."""

import sparkgap
from sparkgap import (
    BANDS,
    CW_BAND_PLAN_R2,
    CW_SEGMENT_TOP_KHZ,
    InstanceManager,
    _cw_skip_zones,
    _derive_cw_window,
)


def test_old_window_unchanged() -> None:
    assert _derive_cw_window(7090) == (7000.0, 7070.0)
    assert _derive_cw_window(14090) == (14000.0, 14070.0)


def test_whole_segment_tops() -> None:
    assert _derive_cw_window(7090, whole_segment=True) == (7000.0, 7125.0)
    assert _derive_cw_window(14090, whole_segment=True) == (14000.0, 14150.0)
    assert set(CW_SEGMENT_TOP_KHZ) == {row[0] for row in CW_BAND_PLAN_R2} == set(BANDS.values())
    for lo, _, hi_with_ft4, _ in CW_BAND_PLAN_R2:
        assert CW_SEGMENT_TOP_KHZ[lo] >= hi_with_ft4


def test_skip_zones() -> None:
    assert _cw_skip_zones(7000, 7125) == [(7073.5, 7081.5)]            # FT8 7074 and JS8 7078, merged
    assert _cw_skip_zones(14000, 14150) == [(14073.5, 14081.5)]        # FT4 14080 is decoded, as before
    assert _cw_skip_zones(14000, 14150, exclude_ft4=True) == [(14073.5, 14083.5)]
    assert _cw_skip_zones(7000, 7125, exclude_ft4=True) == [(7047.0, 7051.0), (7073.5, 7081.5)]
    assert _cw_skip_zones(7000, 7125, exclude_ft8=False) == []
    assert _cw_skip_zones(10100, 10150) == [(10135.5, 10143.5)]        # FT8 10136 and FT4 10140
    assert _cw_skip_zones(21000, 21200) == [(21073.5, 21081.5), (21139.5, 21143.5)]


class _FakeSc:
    def __init__(self) -> None:
        self.zones: list[tuple[float, float]] = []

    def exclude(self, lo_hz: float, hi_hz: float, hold_sec: float) -> bool:
        self.zones.append((lo_hz / 1000.0, hi_hz / 1000.0))
        return hold_sec > 1e6                                          # a zone that never lapses


class _FakeScanner:
    def __init__(self, *a, **k) -> None:
        self._sc = _FakeSc()


def _manager(rate: int, whole: bool, centre: float, monkeypatch) -> InstanceManager:
    monkeypatch.setattr(sparkgap, "_ItilaScanner", _FakeScanner)
    m = InstanceManager(rate, decoder_bin=None, max_instances=0, speeds=[], use_itila=True,
                        cw_whole_segment=whole)
    m.update_signals([], centre)
    return m


def test_scanner_gets_the_window_and_zones(monkeypatch, caplog) -> None:
    m = _manager(192_000, False, 7090.0, monkeypatch)
    assert m.decode_khz == (7000.0, 7070.0) and m._itila_scanner._sc.zones == []
    m = _manager(192_000, True, 7090.0, monkeypatch)
    assert m.decode_khz == (7000.0, 7125.0) and m._itila_scanner._sc.zones == [(7073.5, 7081.5)]
    # a 96 kHz span: up to the flat part (BAND_EDGE_MARGIN) only, never below the old top
    for centre, span, zones in ((7038.4, (7000.0, 7076.8), [(7073.5, 7077.5)]),
                                (7060.0, (7012.0, 7098.4), [(7073.5, 7081.5)]),
                                (7020.0, (7000.0, 7068.0), [])):
        m = _manager(96_000, True, centre, monkeypatch)
        assert all(abs(a - b) < 1e-6 for a, b in zip(m.decode_khz, span)), (centre, m.decode_khz)
        assert m._itila_scanner._sc.zones == zones, centre
    assert "cannot skip" not in caplog.text
