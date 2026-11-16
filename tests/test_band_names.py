"""Band names in bands[]: the band's lower CW edge sits a margin inside the IQ span at any rate."""

import pytest

from sparkgap import BAND_EDGE_MARGIN, BANDS, CW_BAND_PLAN_R2, SparkGap, band_center_hz

EDGE_KHZ: dict[str, int] = {str(k): v for k, v in BANDS.items()}


@pytest.mark.parametrize("rate", [48_000, 96_000, 192_000])
@pytest.mark.parametrize("name", list(BANDS))
def test_band_edge_clears_the_roll_off(name: str, rate: int) -> None:
    low_hz = band_center_hz(name, rate) - rate // 2
    assert EDGE_KHZ[name] * 1000 - low_hz == round(rate * BAND_EDGE_MARGIN)


def test_names_follow_the_band_plan() -> None:
    assert list(BANDS.values()) == [row[0] for row in CW_BAND_PLAN_R2]
    assert band_center_hz("40m", 96_000) == 7_038_400
    assert band_center_hz("20m", 192_000) == 14_076_800


def test_hz_and_dict_entries_unchanged() -> None:
    assert SparkGap._resolve_band("40m", 96_000) == ("40m", 7_038_400)
    assert SparkGap._resolve_band(7_090_000, 96_000) == ("7090000", 7_090_000)
    assert SparkGap._resolve_band({"center_khz": 7090}, 192_000) == ("7090", 7_090_000)
    assert SparkGap._resolve_band({"center_hz": 14_050_000, "name": "20m"}, 48_000) == (
        "20m", 14_050_000)


@pytest.mark.parametrize("rate", [96_000, 192_000])
def test_cw_windows_fit_the_flat_part(rate: int) -> None:
    flat_hz = rate * (1 - 2 * BAND_EDGE_MARGIN)
    for lo, _, hi_with_ft4, _ in CW_BAND_PLAN_R2:
        if lo not in (1810, 50000):  # 160m and 6m are wider than a 96 kHz span; their bottoms stay anchored
            assert (hi_with_ft4 - lo) * 1000 <= flat_hz
