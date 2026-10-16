"""docs/GUI_FEED_PROTOCOL.md matches what the feed sends: message fields and the spectrum layout."""

from __future__ import annotations

import json
import logging
import re
import socket
import time
from pathlib import Path
from typing import Any

import pytest

import feed_server
from feed_server import FEED_VERSION, PROTOCOL, SPECTRUM_HEADER, FeedServer
from sparkgap import _start_gui_feed

DOC = (Path(__file__).resolve().parent.parent / "docs" / "GUI_FEED_PROTOCOL.md").read_text()


def doc_examples() -> dict[str, dict[str, Any]]:
    """The doc's JSON examples by type; client messages included."""
    out = {}
    for block in re.findall(r"```json\n(.*?)```", DOC, re.S):
        msg = json.loads(block)
        out[msg["type"]] = msg
    return out


EXAMPLES = doc_examples()


@pytest.fixture
def feed():
    f = FeedServer({"bind": "127.0.0.1", "port": 0}, {})
    f.start()
    yield f
    f.stop()


def sent(feed: FeedServer, kind: str) -> dict[str, Any]:
    if kind == "window":
        feed.publish_window(7021.2, "CQ DE W1AW W1AW K", 22, 18.0, 0.4)
        kept = feed.windows
    else:
        feed.publish_spot(7021.2, "W1AW", 18.0, 22)
        kept = feed.spots
    t = time.monotonic()
    while not kept:
        assert time.monotonic() - t < 3
        time.sleep(0.01)
    return kept[-1]


@pytest.mark.parametrize("kind", ["window", "spot"])
def test_live_message_fields(feed: FeedServer, kind: str) -> None:
    assert set(EXAMPLES[kind]) == set(sent(feed, kind))


def test_snapshot_fields(feed: FeedServer) -> None:
    sent(feed, "window")
    sent(feed, "spot")
    snap, doc = feed.snapshot(), EXAMPLES["snapshot"]
    assert set(doc) == set(snap)
    assert set(doc["signals"][0]) == set(snap["signals"][0])
    assert set(doc["windows"][0]) == set(snap["windows"][0])
    assert set(doc["spots"][0]) == set(snap["spots"][0])


def test_status_fields(feed: FeedServer) -> None:
    # as sparkgap._publish_gui_status builds it on the C receiver; the feed adds its own counters
    import sparkgap

    radio = sparkgap._CReceiver("192.0.2.1", 1024, 1, 192_000).health()
    c = socket.create_connection(("127.0.0.1", feed.port))  # a console, for the clients list
    c.sendall(b"GET /feed HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
              b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
    t = time.monotonic()
    while not feed.clients:
        assert time.monotonic() - t < 3
        time.sleep(0.01)
    feed.proc = feed_server.ProcessStats()
    logging.getLogger("sparkgap").error("an error for the problems list")
    prev, sparkgap._gui_feed = sparkgap._gui_feed, feed
    try:
        sparkgap._publish_gui_status([], 3, 120.0, {
            "radio": radio, "telnet": {"port": 7300, "clients": 0}, "iq_trimmed_s": 0.0,
            "recording": {"file": "/tmp/x.wav", "bytes": 44}})
    finally:
        sparkgap._gui_feed = prev
    while feed.status is None:
        assert time.monotonic() - t < 3
        time.sleep(0.01)
    status, doc = feed.status, EXAMPLES["status"]
    assert set(doc) == set(status)
    assert set(doc["radio"]) == set(status["radio"])
    assert set(doc["clients"][0]) == set(status["clients"][0])
    assert set(doc["problems"]) == set(status["problems"])
    assert set(doc["problems"]["last"][0]) == set(status["problems"]["last"][0])
    c.close()


def test_hello_fields() -> None:
    import sparkgap

    cfg = {"gui_feed": {"bind": "127.0.0.1", "port": 0}, "callsign": "W1AW"}
    _start_gui_feed(cfg, [("40m", 7_048_000.0)], 96_000, [])
    try:
        hello, doc = sparkgap._gui_feed.hello, EXAMPLES["hello"]
        assert set(doc) == set(hello)
        assert set(doc["config"]) == set(hello["config"])
        assert set(doc["bands"][0]) == set(hello["bands"][0])
        assert set(doc["run"]) == set(hello["run"])
        assert doc["v"] == FEED_VERSION
    finally:
        sparkgap._gui_feed.stop()
        sparkgap._gui_feed = None


def test_spectrum_layout() -> None:
    rows = re.findall(r"^\| (\d+) \| (u32|f64|f32|u16|u8[^|]*) \|", DOC, re.M)
    offsets = [int(o) for o, _ in rows]
    codes = {"u32": "I", "f64": "d", "f32": "f", "u16": "H"}
    fmt = "<" + "".join(codes[t] for _, t in rows if t in codes)
    assert fmt == SPECTRUM_HEADER.format
    assert offsets[-1] == SPECTRUM_HEADER.size


def test_names() -> None:
    assert f"Protocol `{PROTOCOL}`, feed version {FEED_VERSION}." in DOC
    assert EXAMPLES["subscribe"] == {"type": "subscribe", "spectrum_hz": 20}
