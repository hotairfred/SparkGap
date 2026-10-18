"""gui_feed "spots": the console follows the RBN port (default) or the local port."""
import types

import sparkgap


def _sk(spots=None, local_rule=None):
    cfg = {"gui_feed": {"port": 7320}}
    if spots:
        cfg["gui_feed"]["spots"] = spots
    sk = sparkgap.SparkGap.__new__(sparkgap.SparkGap)
    sk.cfg = cfg
    sk.tracker = types.SimpleNamespace(local_rule=local_rule)
    return sk


def test_default_follows_rbn_port() -> None:
    sk = _sk(local_rule=object())
    assert sk._feed_takes("rbn") and sk._feed_takes("all") and not sk._feed_takes("local")


def test_local_follows_local_port() -> None:
    sk = _sk("local", local_rule=object())
    assert sk._feed_takes("local") and sk._feed_takes("all") and not sk._feed_takes("rbn")


def test_local_without_local_rule_shows_the_rbn_spots() -> None:
    # without local_spot_rule the local port carries telnet_port's spots, so the feed does too
    sk = _sk("local", local_rule=None)
    assert sk._feed_takes("rbn") and sk._feed_takes("all")
