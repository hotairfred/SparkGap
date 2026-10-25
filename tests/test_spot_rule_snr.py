"""spot_rule: the SNR a spot carries (report) while the scanner reading ranks the copies."""

from spot_rule import RepeatSpotRule


def _tier2(call: str) -> int:
    del call
    return 2


def test_the_spot_carries_the_snr_of_the_copy_that_gives_its_frequency() -> None:
    # N7US 2026-10-21: a strong signal's second bin 100 Hz away read 14 dB against 40
    # and its window completed the count
    r = RepeatSpotRule(_tier2, window_s=30.0)
    assert r.feed(r.window_id(1, 0, 0.0), 14032.2, "CQ CWT N7US", 0.0, snr=40.0) == []
    assert r.feed(r.window_id(2, 0, 36.0), 14032.1, "CQ CWT N7US", 36.0, snr=14.0) == [
        ("N7US", 14032.2)
    ]
    assert r.spot_snr["N7US"] == 40.0


def test_report_is_the_snr_a_spot_carries_while_snr_still_picks_the_copy() -> None:
    r = RepeatSpotRule(_tier2)
    assert r.feed(0, 7019.30, "CQ K1ABC K1ABC", 0.0, 30.0, 12.0) == []
    assert r.feed(1, 7019.40, "CQ K1ABC K1ABC", 30.0, 20.0, 15.0) == [("K1ABC", 7019.30)]
    assert r.spot_snr["K1ABC"] == 12.0  # the report of the copy the scanner reading chose
    plain = RepeatSpotRule(_tier2)  # no report: the ranking SNR, as before
    plain.feed(0, 7019.30, "CQ K1ABC K1ABC", 0.0, 30.0)
    plain.feed(1, 7019.40, "CQ K1ABC K1ABC", 30.0, 20.0)
    assert plain.spot_snr["K1ABC"] == 30.0


def test_a_stronger_bin_taking_the_same_audio_keeps_its_report() -> None:
    r = RepeatSpotRule(_tier2)
    r.window_id(1, 10, 0.0)
    r.window_id(2, 10, 0.0)  # windows 0 and 1: two bins, one signal
    r.feed(0, 7019.30, "CQ K1ABC K1ABC", 0.0, 20.0, 8.0)
    r.feed(1, 7019.40, "CQ K1ABC K1ABC", 1.0, 30.0, 11.0)  # same audio, stronger: takes the copy
    r.feed(2, 7019.40, "CQ K1ABC K1ABC", 40.0, 25.0, 10.0)
    assert r.spot_snr["K1ABC"] == 11.0
