"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def no_radio_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test listens for real radio broadcasts: a fake radio that does not answer must never
    send a test to a real radio found on the network."""
    import flex_iq
    monkeypatch.setattr(flex_iq, "discover", lambda seconds=0: {})

    def no_answer(ip: str, timeout: float = 0) -> dict[str, str]:
        raise OSError(f"tests ask no radio ({ip})")

    monkeypatch.setattr(flex_iq, "ask_info", no_answer)


@pytest.fixture(autouse=True)
def no_clock_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test runs timedatectl. ProcessStats starts a daemon thread (gui-clock) that calls clock_synced
    every CLOCK_CHECK_S for the rest of the run; a later test that swaps in a fake subprocess.run and counts
    its calls then counted that one too (test_wspr: 3 decoder runs for 2, main 11-16 04:52Z, Python 3.12)."""
    import feed_server
    monkeypatch.setattr(feed_server, "clock_synced", lambda: None)
