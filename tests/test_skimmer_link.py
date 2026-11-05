"""A live copy links to the server's hub as skimmer <config name> and stops on its stop command."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import process_link
import sparkgap


class FakeLink:
    made: list[FakeLink] = []

    def __init__(self, role: str, name: str, on_command: Any = None) -> None:
        self.role, self.name, self.on_command = role, name, on_command
        self.sent: dict[str, Any] = {}
        FakeLink.made.append(self)

    def start(self) -> FakeLink:
        return self

    def status(self, **fields: Any) -> None:
        self.sent.update(fields)


def test_the_link_names_the_copy_and_stops_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_link, "ProcessLink", FakeLink)
    monkeypatch.setattr(sparkgap, "_config_path", "/run/config/Lake-2.local.json")
    monkeypatch.setattr(sparkgap, "_gui_feed", SimpleNamespace(port=7324, server=object()))
    monkeypatch.setattr(sparkgap, "_link", None)
    skimmer = SimpleNamespace(running=True, cfg={"telnet_port": 7301})
    rx = [{"band": "20m", "centre_khz": 14038.4, "rx": 0}]
    sparkgap._start_link(skimmer, rx)  # pyright: ignore[reportPrivateUsage]
    link = FakeLink.made[-1]
    assert (link.role, link.name) == ("skimmer", "Lake-2")
    assert link.sent == {"state": "live", "receivers": rx, "feed_port": 7324, "telnet_port": 7301,
                         "spots": 0}
    assert link.on_command("restart", {}) == (False, "unknown command restart")
    assert skimmer.running
    assert link.on_command("stop", {}) == (True, "stopping")
    assert not skimmer.running


def test_a_feed_that_did_not_start_has_no_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_link, "ProcessLink", FakeLink)
    monkeypatch.setattr(sparkgap, "_config_path", "Dallas-1.local.json")
    monkeypatch.setattr(sparkgap, "_gui_feed", SimpleNamespace(port=7321, server=None))
    monkeypatch.setattr(sparkgap, "_link", None)
    sparkgap._start_link(SimpleNamespace(running=True, cfg={}), [])  # pyright: ignore[reportPrivateUsage]
    assert FakeLink.made[-1].sent["feed_port"] is None
    assert FakeLink.made[-1].sent["telnet_port"] == 7300
