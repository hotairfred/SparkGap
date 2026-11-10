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
