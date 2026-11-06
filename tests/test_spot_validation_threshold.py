"""spot_validation_threshold: the score a call needs, in place of the validation level's own."""
import pytest

import sparkgap
from spot_rule import VALIDATION, RepeatSpotRule


def _tier(call: str) -> int:
    del call
    return 2


def test_unset_keeps_the_level() -> None:
    assert RepeatSpotRule(_tier, validation="normal").threshold == VALIDATION["normal"]
    assert RepeatSpotRule(_tier).threshold is None


def test_set_replaces_the_level_and_needs_validation_on() -> None:
    assert RepeatSpotRule(_tier, validation="normal", validation_threshold=2.5).threshold == 2.5
    assert RepeatSpotRule(_tier, validation=None, validation_threshold=2.5).threshold is None


@pytest.mark.parametrize("value, want", [(None, VALIDATION["normal"]), (2.5, 2.5), ("2.5", 2.5)])
def test_config_key(value, want) -> None:
    cfg = {"spot_rule": "repeat", "spot_validation": "normal"}
    if value is not None:
        cfg["spot_validation_threshold"] = value
    tracker = type("T", (), {"_matches_patt3ch": lambda self, c: None})()
    assert sparkgap._make_spot_rule(cfg, tracker).threshold == want
