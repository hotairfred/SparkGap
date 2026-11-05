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
