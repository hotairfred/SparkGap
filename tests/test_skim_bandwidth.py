"""Band ranges advertised in SETT and to RBN: skim_bandwidth_khz, else the receiver's span."""
import sparkgap


def test_unset_follows_the_receiver_span() -> None:
    assert sparkgap._skim_bandwidth_khz({"sample_rate": 192000}) == 192.0
    assert sparkgap._skim_bandwidth_khz({"sample_rate": 96000}) == 96.0


def test_set_value_wins() -> None:
    assert sparkgap._skim_bandwidth_khz({"sample_rate": 192000, "skim_bandwidth_khz": 180}) == 180.0
