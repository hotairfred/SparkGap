"""Settings the GUI feed reports: values in effect, defaults, behaviour built into the rule."""

from sparkgap import _gui_settings


def test_empty_config_reports_defaults() -> None:
    eff, defaults, built_in = _gui_settings({}, 48_000)
    assert eff["cw_decoder"] == "itila2" and eff["telnet_port"] == 7300
    assert eff["enable_ft8"] is True
    assert eff["spot_rule"] == "repeat" and eff["spot_rule_cq_groups"] is True
    assert eff["flex_daxiq_channel"] is None  # not a Flex config
    assert "telnet_port" in defaults and "sample_rate" not in defaults
    assert set(built_in) == {"spot_rule_cq_groups", "spot_rule_lockon", "spot_rule_cq_qualifiers",
                             "spot_rule_split_runs", "spot_rule_runner_signs", "spot_rule_window_join",
                             "spot_rule_owner_listed", "spot_rule_takeover_idle_s"}
    assert eff["spot_rule_owner_listed"] is False      # no known-call list
    assert eff["spot_rule_takeover_idle_s"] == 180.0


def test_itila2_runs_the_repeat_rule_by_default() -> None:
    eff, defaults, _ = _gui_settings({"cw_decoder": "itila2"}, 96_000)
    assert eff["spot_rule"] == "repeat" and "spot_rule" in defaults
    assert eff["spot_rule_lockon"] is True and eff["spot_rule_split_runs"] is True    # built in


def test_repeat_rule_with_rbn_validation() -> None:
    cfg = {"spot_rule": "repeat", "spot_validation": "normal", "spot_validation_calls": "rbn",
           "sdr_type": "flex", "spot_rule_split_runs": True}
    eff, defaults, _ = _gui_settings(cfg, 96_000)
    assert eff["spot_rule_lockon"] is True and eff["spot_rule_split_runs"] is True
    assert eff["spot_rule_owner_listed"] is True       # with the RBN list
    assert eff["rbn_known_days"] == 7 and "rbn_known_days" in defaults
    assert eff["flex_daxiq_channel"] == 1 and "flex_daxiq_channel" in defaults


def test_validation_off_by_default() -> None:
    eff, _, _ = _gui_settings({}, 48_000)
    assert eff["spot_validation"] == "off" and eff["rbn_known_days"] is None


def test_calls_ignored_without_validation() -> None:
    eff, _, _ = _gui_settings({"spot_validation_calls": "rbn"}, 48_000)
    assert eff["spot_validation_calls"] == "off" and eff["rbn_known_days"] is None


def test_validation_off_is_accepted() -> None:
    from sparkgap import _make_spot_rule
    eff, _, _ = _gui_settings({"spot_validation": "off", "spot_validation_calls": "rbn"}, 48_000)
    assert eff["spot_validation"] == "off" and eff["spot_validation_calls"] == "off"
    assert _make_spot_rule({"spot_validation": "off"}, None).threshold is None


def test_spot_rule_and_decoder_keys_are_reported() -> None:
    cfg = {"spot_rule_fragments": "any,stray", "spot_rule_owner_cell": True, "itila_window_sec": 30}
    eff, defaults, _ = _gui_settings(cfg, 48_000)
    assert eff["spot_rule_fragments"] == "any,stray" and eff["spot_rule_owner_cell"] is True
    assert eff["spot_rule_owned_two_windows"] is False and "spot_rule_owned_two_windows" in defaults
    assert eff["itila_window_sec"] == 30.0 and "itila_window_sec" not in defaults
    assert eff["itila_max_bins"] == 200 and eff["itila_decode_threads"] == 1
    eff, _, _ = _gui_settings({"spot_rule": "none", "spot_rule_fragments": "any"}, 48_000)
    assert eff["spot_rule_fragments"] == "off" and eff["spot_rule_contest_words"] is False


def test_afc_is_reported_for_itila2() -> None:
    eff, defaults, _ = _gui_settings({"itila2_afc": True}, 48_000)
    assert eff["itila2_afc"] is True and "itila2_afc" not in defaults
    eff, defaults, _ = _gui_settings({}, 48_000)
    assert eff["itila2_afc"] is False and "itila2_afc" in defaults
    assert _gui_settings({"cw_decoder": "itila"}, 48_000)[0]["itila2_afc"] is None


def test_the_gated_rule_is_reported_with_the_level_it_applies() -> None:
    eff, _, _ = _gui_settings({"spot_rule_gates": True, "spot_validation_calls": "rbn"}, 48_000)
    assert eff["spot_rule"] == "gates" and eff["spot_rule_cq_groups"] is False
    assert eff["spot_validation"] == "normal" and eff["spot_validation_calls"] == "rbn"
    assert _gui_settings({"spot_rule_gates": True, "spot_rule": "off"}, 48_000)[0]["spot_rule"] == "off"


def test_the_gated_rule_takes_6_db_at_gate_9_unless_set() -> None:
    from sparkgap import _make_spot_rule
    tracker = type("T", (), {"_matches_patt3ch": lambda self, c: "active"})()
    assert _make_spot_rule({"spot_rule_gates": True}, tracker).stronger_db == 6.0
    assert _make_spot_rule({"spot_rule_gates": True, "spot_rule_strongest_db": 10}, tracker).stronger_db == 10.0
    for off in (None, 0):
        assert _make_spot_rule({"spot_rule_gates": True, "spot_rule_strongest_db": off}, tracker).stronger_db is None


def test_the_start_up_line_names_the_rule_and_every_option_on() -> None:
    """cdub89/SparkGap#45: the Gated Rule by name; the Repeat Rule with every option it runs."""
    from sparkgap import _spot_rule_line
    from spot_rule import RepeatSpotRule
    from spot_rule2 import GateSpotRule
    assert _spot_rule_line(GateSpotRule(lambda c: "active")) == "Spot rule: Gated Rule"
    rule = RepeatSpotRule(lambda c: 2, cq_groups=True, lockon=True, cq_qualifiers=True, split_runs=True,
                          owner_cell=True, la1d=True, gluek=True, norival=True, contest_words=True,
                          fragments="any,stray", strongest_db=6.0, lock_idle_s=600.0)
    line = _spot_rule_line(rule)
    assert line.startswith("Spot rule: Repeat Rule: lock-on, CQ groups, CQ qualifiers, split runs, DE rejoin")
    for name in ("contest words", "owner cell", "LA1D", "glue K", "no rival", "fragments any, stray",
                 "strongest 6 dB", "lock idle 600 s"):
        assert name in line, name
    assert "garbled runner" not in line and "clip busts" not in line


def test_decoder_snr_is_on_with_itila2_unless_set() -> None:
    from sparkgap import _snr_decoder_on
    assert _snr_decoder_on({}) and _snr_decoder_on({"cw_decoder": "itila2"})
    assert not _snr_decoder_on({"cw_decoder": "itila"})
    assert not _snr_decoder_on({"itila2_snr_decoder": False})
    assert _snr_decoder_on({"cw_decoder": "itila", "itila2_snr_decoder": True})


def test_why_answers_from_the_gate_rule() -> None:
    """cdub89/SparkGap#51 R10: the answer to SparkGap Web's why message."""
    from sparkgap import _why_answer
    from spot_rule import RepeatSpotRule
    from spot_rule2 import GateSpotRule
    rule = GateSpotRule(lambda c: "active")
    win = rule.window_id(1, 1, 0.0)
    rule.feed(win, 7031.4, "K1VUT K1VUT", 0.0)                  # no keyword: gate 5
    assert _why_answer(rule, "K1VUT", 7031.0) == "Not heard calling CQ"
    assert _why_answer(rule, "W1AW", 7031.0) == "No decision on this call yet"
    assert _why_answer(RepeatSpotRule(lambda c: "active"), "K1VUT", 7031.0) == "No reasons under the Repeat Rule"
    assert _why_answer(None, "K1VUT", 7031.0) == "No spot rule running"


def test_a_spot_s_reason_is_taken_even_when_it_does_not_go_out() -> None:
    """A blacklisted call's reason is taken too, so spot_why does not grow over a long run."""
    from types import SimpleNamespace
    import sparkgap
    from spot_rule2 import GateSpotRule
    rule = GateSpotRule(lambda c: "active")
    t = sparkgap.SpotTracker.__new__(sparkgap.SpotTracker)
    t.spot_rule, t.local_rule, t.blacklist = rule, None, {"K1ABC"}
    rule.feed(rule.window_id(1, 1, 0.0), 7030.0, "CQ CQ DE K1ABC K1ABC K", 0.0)
    t._pass_intents = {id(rule): {7030.0: SimpleNamespace(snr_db=10.0, wpm=20)}}
    assert t.flush_pass() == [] and rule.spot_why == {}
