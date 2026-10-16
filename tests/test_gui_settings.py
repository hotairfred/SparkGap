"""Settings the GUI feed reports: values in effect, defaults, behaviour built into the rule."""

from sparkgap import _gui_settings


def test_empty_config_reports_defaults() -> None:
    eff, defaults, built_in = _gui_settings({}, 48_000)
    assert eff["cw_decoder"] == "itila" and eff["telnet_port"] == 7300
    assert eff["enable_ft8"] is True
    assert eff["spot_rule"] == "off" and eff["spot_rule_cq_groups"] is False
    assert eff["flex_daxiq_channel"] is None  # not a Flex config
    assert "telnet_port" in defaults and "sample_rate" not in defaults
    assert set(built_in) == {"spot_rule_cq_groups", "spot_rule_lockon", "spot_rule_cq_qualifiers"}


def test_itila2_runs_the_repeat_rule_by_default() -> None:
    eff, defaults, _ = _gui_settings({"cw_decoder": "itila2"}, 96_000)
    assert eff["spot_rule"] == "repeat" and "spot_rule" in defaults
    assert eff["spot_rule_lockon"] is True and eff["spot_rule_split_runs"] is False


def test_repeat_rule_on_a_flex() -> None:
    cfg = {"spot_rule": "repeat", "sdr_type": "flex", "spot_rule_split_runs": True}
    eff, defaults, _ = _gui_settings(cfg, 96_000)
    assert eff["spot_rule_lockon"] is True and eff["spot_rule_split_runs"] is True
    assert eff["flex_daxiq_channel"] == 1 and "flex_daxiq_channel" in defaults
