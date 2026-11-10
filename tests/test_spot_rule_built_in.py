"""cdub89/SparkGap#46: split runs, runner signs, window join, owner listed and a 180 s takeover are
built into the repeat rule; spot_validation_threshold is removed.  A config that still sets one is told."""

import logging

import sparkgap


def _tracker():
    return type("T", (), {"_matches_patt3ch": lambda self, c: None})()


def test_built_in_whatever_the_keys_say() -> None:
    for cfg in ({}, {k: False for k in sparkgap.BUILT_IN_KEYS}):
        rule = sparkgap._make_spot_rule(dict(cfg, spot_rule="repeat"), _tracker())
        assert rule.split_runs and rule.runner_signs and rule.window_join
        assert rule.takeover_idle_s == sparkgap.TAKEOVER_IDLE_S == 180.0


def test_owner_listed_follows_the_call_list(monkeypatch) -> None:
    monkeypatch.setattr(sparkgap, "_validation_list", lambda config, tracker, calls: (lambda c: True) if calls else None)
    base = {"spot_rule": "repeat", "spot_validation": "normal"}
    assert not sparkgap._make_spot_rule(base, _tracker()).owner_listed
    assert sparkgap._make_spot_rule(dict(base, spot_validation_calls="rbn"), _tracker()).owner_listed


def test_retired_keys_are_noted(caplog) -> None:
    caplog.set_level(logging.INFO, logger="sparkgap")
    sparkgap._note_retired_keys({"spot_rule_split_runs": True, "spot_rule_window_join": False,
                                 "spot_validation_threshold": 2.5, "spot_rule_cq_repeats": True})
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("spot_rule_split_runs: built into") and "turned off" not in m for m in msgs)
    assert any(m.startswith("spot_rule_window_join: built into") and "can't be turned off" in m for m in msgs)
    assert any(m.startswith("spot_validation_threshold: removed") for m in msgs)
    assert not any("cq_repeats" in m for m in msgs)
    assert [r.levelname for r in caplog.records if "threshold" in r.getMessage()] == ["WARNING"]
