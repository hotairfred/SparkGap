"""The Gated Rule's MASTER.SCP filter (spot_rule_scp) reads MASTER.SCP, cdub89/SparkGap#58."""

from __future__ import annotations

import logging

import pytest

import sparkgap


def _tracker(scp: set[str]) -> object:
    # valid_calls: MASTER.SCP, as load_callsign_db reads it
    return type("T", (), {"_matches_patt3ch": lambda self, c: "active", "valid_calls": scp})()


def test_the_known_call_filter_reads_master_scp() -> None:
    cfg = {"spot_rule_gates": True, "spot_rule_scp": True}
    rule = sparkgap._make_spot_rule(cfg, _tracker({"K1ABC"}))
    assert rule.scp is not None and rule.scp("K1ABC") and rule.scp("K1ABC/7")
    assert not rule.scp("K1ABD")
    off = dict(cfg, spot_rule_scp=False)
    assert sparkgap._make_spot_rule(off, _tracker({"K1ABC"})).scp is None


def test_the_known_call_filter_without_master_scp_is_off(
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="sparkgap")
    cfg = {"spot_rule_gates": True, "spot_rule_scp": True}
    rule = sparkgap._make_spot_rule(cfg, _tracker(set()))
    assert rule.scp is None and "the MASTER.SCP filter is off" in caplog.text


def test_the_known_call_filter_without_the_gate_rule_says_so(
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="sparkgap")
    sparkgap._make_spot_rule({"spot_rule": "repeat", "spot_rule_scp": True}, _tracker(set()))
    assert "the MASTER.SCP filter acts only with the gate rule" in caplog.text
