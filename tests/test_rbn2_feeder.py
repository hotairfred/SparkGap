"""rbn2_feeder: the auth library's contract (through a fake, never a real library), fail closed,
posts on the upload tick, and the sign-in."""

import ctypes
import os
import queue
import signal
import threading
import logging
import zlib
from types import SimpleNamespace
from typing import Any

import pytest

import rbn2_feeder as f


def fake_hash(tag: str, text: str) -> str:
    return f"{zlib.crc32((tag + text).encode()):08x}"


class FakeFn:
    """A library function: argtypes and restype settable, like a ctypes function."""

    def __init__(self, impl: Any) -> None:
        self.impl, self.argtypes, self.restype = impl, None, None

    def __call__(self, *args: Any) -> int:
        return self.impl(*args)


class FakeLib:
    def __init__(
        self,
        ret: int = 0,
        refuse: str = "",
        version: bytes = b"rbn2auth/1.2",
        bad_hash: bytes | None = None,
    ) -> None:
        def ver(buf: Any, _n: int) -> int:
            buf.value = version
            return ret

        def hashed(tag: str) -> Any:
            def h(text: bytes, out: Any, _n: int) -> int:
                if not text or text.decode() == refuse:
                    return -1
                out.value = (
                    bad_hash if bad_hash is not None else fake_hash(tag, text.decode()).encode()
                )
                return 0

            return h

        def spot(calls: Any, n: int, out: Any, _n: int) -> int:
            got = [calls[i].decode() for i in range(n)]
            if not got or refuse in got or "" in got:
                return -1
            out.value = bad_hash if bad_hash is not None else fake_hash("h", "|".join(got)).encode()
            return 0

        self.rbn2_auth_version = FakeFn(ver)
        self.rbn2_auth_short_hash = FakeFn(hashed("s"))
        self.rbn2_auth_spot_hash = FakeFn(spot)


def _auth(**kw: Any) -> f.Auth:
    return f.Auth("librbn2_auth.so", loader=lambda _p: FakeLib(**kw))


def _session(auth: f.Auth | None, dry_run: bool = False) -> f.Session:
    return f.Session(
        url="http://rbn.invalid/rx/6/",
        call="N0CALL-1",
        name="SparkGap",
        grid="FN31pr",
        qth="",
        validation="Normal",
        port=7300,
        bands=[(7000.0, 7096.0)],
        auth=auth,
        version=f.SKIM_VERSION,
        fingerprint="f" * 32,
        dry_run=dry_run,
    )


SPOT = {"freq": 7027.64, "call": "JA0FVU", "cq_flag": "CQ", "snr": 25, "wpm": 30, "mode": "CW"}


def test_the_library_is_set_up_and_reports_its_version() -> None:
    auth = _auth()
    assert auth.version == "rbn2auth/1.2"
    assert auth._short.argtypes == [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
    assert auth._spot.argtypes == [
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]


def test_another_version_is_refused() -> None:
    with pytest.raises(f.AuthError, match="version 'rbn2auth/7a2eb2c'"):
        _auth(version=b"rbn2auth/7a2eb2c", ret=1)  # the one-call library
    with pytest.raises(f.AuthError, match="version 'rbn2auth/1.3'"):
        _auth(version=b"rbn2auth/1.3")


@pytest.mark.parametrize("ret", [-1, 1])
def test_a_version_call_that_does_not_return_0_is_refused(ret: int) -> None:
    with pytest.raises(f.AuthError, match=f"returned {ret}"):
        _auth(ret=ret)  # writes rbn2auth/1.2 all the same


def test_a_missing_library_is_refused() -> None:
    def missing(path: str) -> Any:
        raise OSError(f"{path}: cannot open shared object file")

    with pytest.raises(f.AuthError, match="cannot load"):
        f.Auth("/nowhere/librbn2_auth.so", loader=missing)


def test_a_library_without_a_function_is_refused() -> None:
    lib = FakeLib()
    del lib.rbn2_auth_spot_hash
    with pytest.raises(f.AuthError, match="cannot load"):
        f.Auth("librbn2_auth.so", loader=lambda _p: lib)


@pytest.mark.parametrize("version", [b"", b"\xff\xfe", b"rbn2\x01auth"])
def test_an_unusable_version_string_is_refused(version: bytes) -> None:
    with pytest.raises(f.AuthError, match="version"):
        _auth(version=version)


@pytest.mark.parametrize("bad", [b"", b"abc", b"zzzzzzzz", b"0123456789", b"\xff" * 8])
def test_a_malformed_hash_is_a_refusal(bad: bytes) -> None:
    sess = _session(_auth(bad_hash=bad))
    assert sess.spots_body([SPOT], 0.0) is None
    assert sess.id_body() is None


def test_the_library_is_looked_for_in_the_run_root() -> None:
    from pathlib import Path

    assert f.auth_lib_path() == Path(f.__file__).resolve().parent.parent / "librbn2_auth.so"
    assert str(f.auth_lib_path()) in (f.not_ready(None, dry_run=False) or "")


def test_without_a_library_nothing_is_posted() -> None:
    assert f.not_ready(None, dry_run=False)
    assert f.not_ready(_auth(), dry_run=False) is None
    assert f.not_ready(None, dry_run=True) is None


def test_a_session_without_a_library_may_only_dry_run() -> None:
    with pytest.raises(f.AuthError):
        _session(None)
    assert _session(None, dry_run=True).agg == "6.7"


def test_one_spot_is_hashed_as_a_list_of_one() -> None:
    sess = _session(_auth())
    body = sess.spots_body([SPOT], 1_761_000_000.0)
    assert body is not None
    assert body["h"] == fake_hash("h", "JA0FVU")
    assert body["agg"] == "6.7" and body["e"] == "N0CALL-1"
    assert body["s"] == [["7027.64", "JA0FVU", "1", "25", "30", "dB", "1", "CQ", "D:"]]


def test_one_hash_covers_the_post_s_calls_in_row_order() -> None:
    sess = _session(_auth())
    body = sess.spots_body([SPOT, {**SPOT, "call": "K1ABC", "freq": 7031.0}], 0.0)
    assert body is not None and body["h"] == fake_hash("h", "JA0FVU|K1ABC")
    assert [r[:2] for r in body["s"]] == [["7027.64", "JA0FVU"], ["7031.00", "K1ABC"]]
    assert sess.auth is not None and sess.auth.spot_hash([]) is None


def test_a_refused_call_drops_the_spot() -> None:
    assert _session(_auth(refuse="JA0FVU")).spots_body([SPOT], 0.0) is None
    both = [SPOT, {**SPOT, "call": "K1ABC"}]
    assert _session(_auth(refuse="K1ABC")).spots_body(both, 0.0) is None


def test_the_sign_in_names_sparkgap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f, "ntp_synced", lambda: True)
    sess = _session(_auth())
    body = sess.id_body()
    assert body is not None
    sign_in = "SparkGap 1.2 is operated by SparkGap, N0CALL-1"
    assert body["skimSignIn"] == sign_in and body["shortHash"] == fake_hash("s", sign_in)
    assert body["skimVersion"] == "SparkGap 1.2" and body["aggVersion"] == "6.7"
    assert body["justCQ"] == "1" and body["ClockNtp"] is True
    # as Aggregator 6.7 for a CW source (hotairfred/SparkGap#148): bandLimits and cWBandLimits, no MIXED
    assert body["bandLimits"] == body["cWBandLimits"] == "7000.0-7096.0"
    assert body["mIXEDBandLimits"] == "" and body["rTTYBandLimits"] == ""
    spot = sess.spots_body([SPOT], 0.0)
    assert spot is not None and spot["nTP"] is True  # as the last id.php reported


def test_a_refused_sign_in_sends_no_id_and_no_spots(monkeypatch: pytest.MonkeyPatch) -> None:
    sess = _session(_auth(refuse="SparkGap 1.2 is operated by SparkGap, N0CALL-1"))
    posts: list[str] = []
    monkeypatch.setattr(sess, "post", lambda path, body: posts.append(path) or {})
    assert sess.post_id() is False and posts == [] and not sess.registered.is_set()


def test_a_failed_id_stops_spots_until_one_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sess = _session(_auth())
    replies: list[dict[str, Any] | None] = [{}, None, {}]
    monkeypatch.setattr(sess, "post", lambda path, body: replies.pop(0))
    assert sess.post_id() and sess.registered.is_set()
    assert not sess.post_id() and not sess.registered.is_set()
    assert sess.post_id() and sess.registered.is_set()


def _run_upload(
    sess: f.Session, calls: list[str], blacklist: set[str], posts: int
) -> list[dict[str, Any]]:
    """Queue the calls, run the loop on a fast tick until it has made `posts` posts (or 1 s)."""
    sent: list[dict[str, Any]] = []
    spots: queue.Queue[dict[str, Any]] = queue.Queue()
    for call in calls:
        spots.put({**SPOT, "call": call})
    shutdown = threading.Event()

    def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        sent.append(body)
        if len(sent) >= posts:
            shutdown.set()
        return {}

    sess.post = post  # type: ignore[method-assign]
    sess.upload_s = 0.01
    t = threading.Thread(target=f.upload_loop, args=(sess, spots, blacklist, shutdown, lambda: 0.0))
    t.start()
    t.join(1.0)
    shutdown.set()
    t.join()
    return sent


def test_a_tick_s_spots_go_in_one_post() -> None:
    sess = _session(_auth())
    sess.registered.set()
    sent = _run_upload(sess, ["JA0FVU", "N0CALL", "K1ABC"], {"N0CALL"}, posts=1)  # blacklisted
    assert [[r[1] for r in b["s"]] for b in sent] == [["JA0FVU", "K1ABC"]]


def test_every_spot_queued_goes_in_one_post() -> None:
    sess = _session(_auth())
    sess.registered.set()
    calls = [f"K{i}AA" for i in range(70)]
    sent = _run_upload(sess, calls, set(), posts=1)
    assert len(sent) == 1 and [r[1] for r in sent[0]["s"]] == calls
    assert sent[0]["h"] == fake_hash("h", "|".join(calls))


def test_the_tick_follows_the_server_s_upload_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    sess = _session(_auth())
    replies: list[dict[str, Any]] = [
        {"serverUploadInterval": 20},
        {"serverUploadInterval": "x"},
        {},
    ]
    monkeypatch.setattr(sess, "post", lambda path, body: replies.pop(0))
    assert sess.upload_s == f.UPLOAD_S
    sess.post_id()
    assert sess.upload_s == 20.0
    sess.post_id()
    sess.post_id()
    assert sess.upload_s == 20.0


def test_the_id_reply_is_logged_with_hidden_fields_masked(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    sess = _session(_auth())
    echo = {"Limit3Min": 40, "skimSignIn": "x is operated by y", "shortHash": "1234abcd"}
    monkeypatch.setattr(sess, "post", lambda path, body: echo)
    caplog.set_level("INFO")
    sess.post_id()
    assert "Limit3Min" in caplog.text and "40" in caplog.text
    assert "x is operated by y" not in caplog.text and "1234abcd" not in caplog.text


def test_no_spot_goes_out_before_the_sign_in_succeeds() -> None:
    sess = _session(_auth())
    assert _run_upload(sess, ["JA0FVU", "K1ABC"], set(), posts=1) == []


def test_a_reply_that_is_not_json_text_counts_as_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    class Resp:
        def __enter__(self) -> "Resp":
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def read(self) -> bytes:
            return b"\xff\xfe not json"

    monkeypatch.setattr(f.urlreq, "urlopen", lambda req, timeout: Resp())
    assert _session(_auth()).post("s.php", {"t": "s"}) == {}


SPOT_LINE = (
    b"DX de N0CALL-1-#:  14025.00  K7WP         CW   21 dB 27 WPM  CQ"
    b"                  SG  1945Z\a\r\n"
)


class FakeSock:
    """recv replies in order: bytes, or an exception to raise."""

    def __init__(self, replies: list[Any], shutdown: threading.Event) -> None:
        self.replies, self.shutdown, self.sent = replies, shutdown, b""

    def __enter__(self) -> "FakeSock":
        return self

    def __exit__(self, *a: Any) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, n: int) -> bytes:
        if not self.replies:
            self.shutdown.set()
            return b""
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def _read(replies: list[Any]) -> tuple[list[dict[str, Any]], int]:
    shutdown = threading.Event()
    spots: queue.Queue[dict[str, Any]] = queue.Queue()
    sock = FakeSock(replies, shutdown)
    connects = []

    def connect(addr: Any, timeout: float) -> FakeSock:
        connects.append(addr)
        return sock

    f.read_local("127.0.0.1", 7300, "N0CALL-1", spots, shutdown, connect)  # type: ignore[arg-type]
    assert sock.sent == b"N0CALL-1\n"
    return [spots.get() for _ in range(spots.qsize())], len(connects)


def test_a_quiet_minute_keeps_the_connection() -> None:
    got, connects = _read([TimeoutError(), TimeoutError(), SPOT_LINE])
    assert [s["call"] for s in got] == ["K7WP"]
    assert connects == 1


def test_a_spot_split_across_reads_is_put_together() -> None:
    got, _ = _read([SPOT_LINE[:20], SPOT_LINE[20:]])
    assert [s["call"] for s in got] == ["K7WP"]


def test_eof_reconnects_and_reads_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f, "RECONNECT_S", 0.0)
    shutdown = threading.Event()
    spots: queue.Queue[dict[str, Any]] = queue.Queue()
    socks = [FakeSock([b""], threading.Event()), FakeSock([SPOT_LINE], shutdown)]
    f.read_local("127.0.0.1", 7300, "N0CALL-1", spots, shutdown, lambda a, timeout: socks.pop(0))  # type: ignore[arg-type]
    assert not socks
    assert spots.get_nowait()["call"] == "K7WP"


def test_a_failed_thread_stops_the_feeder() -> None:
    shutdown, failed = threading.Event(), threading.Event()

    def boom() -> None:
        raise ValueError("bad reply")

    f.worker(boom, shutdown, failed)()
    assert failed.is_set() and shutdown.is_set()


def test_a_thread_that_ends_unasked_stops_the_feeder() -> None:
    shutdown, failed = threading.Event(), threading.Event()
    f.worker(lambda: None, shutdown, failed)()
    assert failed.is_set() and shutdown.is_set()
    shutdown, failed = threading.Event(), threading.Event()
    shutdown.set()
    f.worker(lambda: None, shutdown, failed)()  # asked to stop: not a failure
    assert not failed.is_set()


def test_a_broken_http_reply_is_a_failed_post(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(req: Any, timeout: float) -> Any:
        raise f.http.client.IncompleteRead(b"")

    monkeypatch.setattr(f.urlreq, "urlopen", urlopen)
    assert _session(_auth()).post("s.php", {"t": "s"}) is None


def test_a_dry_run_logs_no_hash_fingerprint_or_sign_in(caplog: pytest.LogCaptureFixture) -> None:
    sess = _session(_auth(), dry_run=True)
    caplog.set_level("INFO")
    sess.post_id()
    body = sess.spots_body([SPOT], 0.0)
    assert body is not None
    sess.post("s.php", body)
    logged = caplog.text
    assert sess.auth is not None
    secrets = (
        "f" * 32,
        sess.sign_in(),
        sess.auth.short_hash(sess.sign_in()),
        sess.auth.spot_hash(["JA0FVU"]),
    )
    for secret in secrets:
        assert secret and secret not in logged
    assert '"fingerPrint":"<hidden>"' in logged and '"h":"<hidden>"' in logged
    assert '"skimSignIn":"<hidden>"' in logged


SEAT = {
    "callsign": "N0CALL-2",
    "grid": "FN31pr",
    "op_name": "Op",
    "qth": "Town",
    "spot_validation": "permissive",
    "telnet_port": 7302,
    "rbn_upload": "dry",
}


def test_the_seat_config_gives_the_feeder_its_settings() -> None:
    seat = f.seat_settings(SEAT)
    assert (seat.call, seat.grid, seat.name, seat.qth) == ("N0CALL-2", "FN31pr", "Op", "Town")
    assert (seat.validation, seat.port, seat.mode) == ("Permissive", 7302, "dry")
    assert seat.host == "127.0.0.1"
    assert f.seat_settings({**SEAT, "rbn_upload": "live"}).mode == "live"


@pytest.mark.parametrize("mode", [None, "off", "on", True])
def test_upload_is_off_unless_the_config_says_dry_or_live(mode: Any) -> None:
    cfg = {k: v for k, v in SEAT.items() if k != "rbn_upload"}
    with pytest.raises(ValueError, match="off"):
        f.seat_settings(cfg if mode is None else {**cfg, "rbn_upload": mode})


@pytest.mark.parametrize("key", ["callsign", "grid"])
def test_a_seat_without_a_call_or_grid_does_not_feed(key: str) -> None:
    with pytest.raises(ValueError, match="callsign and grid"):
        f.seat_settings({**SEAT, key: ""})


@pytest.mark.parametrize(
    ("level", "sent"),
    [("normal", "Normal"), ("off", "Off"), (False, "Off"), (None, "Off"), ("", "Off")],
)
def test_the_validation_level_is_sparkgap_s_own(level: Any, sent: str) -> None:
    assert f.seat_settings({**SEAT, "spot_validation": level}).validation == sent


def test_validation_absent_is_off_as_in_sparkgap() -> None:
    cfg = {k: v for k, v in SEAT.items() if k != "spot_validation"}
    assert f.seat_settings(cfg).validation == "Off"
    with pytest.raises(ValueError, match="spot_validation"):
        f.seat_settings({**SEAT, "spot_validation": "high"})


@pytest.mark.parametrize(("sig", "name"), [(signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "Ctrl-C")])
def test_a_signal_stops_the_feeder_cleanly_and_says_so(
    sig: signal.Signals, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    shutdown, failed = threading.Event(), threading.Event()
    old_int = signal.signal(signal.SIGINT, signal.default_int_handler)  # as in a feeder process
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        threading.Timer(0.1, os.kill, (os.getpid(), sig)).start()
        with caplog.at_level("INFO", logger="rbn2_feeder"):
            assert f.wait_for_stop(shutdown, failed) is True
    finally:
        signal.signal(signal.SIGINT, old_int)
    assert shutdown.is_set() and f"stopped ({name})" in caplog.text
    assert signal.getsignal(signal.SIGTERM) is old_term  # the old handler is back


def test_a_failed_thread_stops_the_feeder_with_an_error(caplog: pytest.LogCaptureFixture) -> None:
    shutdown, failed = threading.Event(), threading.Event()
    failed.set()
    shutdown.set()
    with caplog.at_level("INFO", logger="rbn2_feeder"):
        assert f.wait_for_stop(shutdown, failed) is False
    assert "stopped: a thread failed" in caplog.text


def test_the_spotter_keeps_a_node_suffix_but_not_the_default() -> None:
    assert f.seat_settings({**SEAT, "callsign": "WF8Z", "skimmer_suffix": "-1"}).call == "WF8Z-1"
    assert f.seat_settings({**SEAT, "callsign": "WF8Z", "skimmer_suffix": "-#"}).call == "WF8Z"
    assert f.seat_settings({**SEAT, "callsign": "WF8Z"}).call == "WF8Z"
    assert f.seat_settings({**SEAT, "skimmer_suffix": ""}).call == "N0CALL-2"


def test_id_php_reads_the_band_list_again(monkeypatch: pytest.MonkeyPatch) -> None:
    # cdub89/SparkGap#33 O1: a seat that changed band reports its new bandLimits at the next id.php
    monkeypatch.setattr(f, "ntp_synced", lambda: True)
    probes = iter([[(14000.0, 14096.0)], [], [(7000.0, 7096.0)]])
    sess = _session(None, dry_run=True)
    sess.bands_of = lambda: next(probes)
    bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(sess, "post", lambda path, body: bodies.append(body) or {})
    assert sess.post_id() and sess.bands == [(14000.0, 14096.0)]   # moved to 20 m
    assert sess.post_id() and sess.bands == [(14000.0, 14096.0)]   # an empty probe keeps the last
    assert sess.post_id() and sess.bands == [(7000.0, 7096.0)]     # back on 40 m
    assert [b["bandLimits"] for b in bodies] == [
        "14000.0-14096.0", "14000.0-14096.0", "7000.0-7096.0"]


def test_the_feeder_counts_warnings_and_errors_for_its_status() -> None:
    levels = f.LevelCount()
    lg = logging.getLogger("test-levels")
    lg.addHandler(levels)
    lg.warning("w")
    lg.error("e")
    lg.error("e")
    assert (levels.warnings, levels.errors) == (1, 2)


def test_the_feeder_sends_its_status_to_the_server() -> None:
    """Mode, spotter, sign-in, bands and the post counts, every STATUS_S (cdub89/SparkGap#33)."""
    sent: list[dict] = []

    class Link:
        def status(self, **fields: object) -> None:
            sent.append(fields)

    sess = SimpleNamespace(call="WX7V-5", registered=threading.Event(),
                           bands=[(3490.4, 3586.4)],
                           counts={"posts": 2, "sent": 5, "not signed in": 1})
    stop = threading.Event()
    stop.set()
    seat = SimpleNamespace(mode="dry")
    f.status_loop(Link(), sess, seat, f.LevelCount(), stop)  # type: ignore[arg-type]
    assert sent == [{"mode": "dry", "spotter": "WX7V-5", "registered": False,
                     "bands": [[3490.4, 3586.4]], "warnings": 0, "errors": 0,
                     "posts": 2, "sent": 5, "not_signed_in": 1}]


def test_rtty_seat_sends_its_ranges_as_rtty_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """enable_rtty: the ranges also go in rTTYBandLimits, as the Aggregator sends an RTTY Skimmer source's."""
    monkeypatch.setattr(f, "ntp_synced", lambda: True)
    sess = _session(_auth())
    sess.rtty = True
    body = sess.id_body()
    assert body is not None
    assert body["bandLimits"] == body["cWBandLimits"] == body["rTTYBandLimits"] == "7000.0-7096.0"
    assert body["mIXEDBandLimits"] == ""
    assert f.seat_settings({"rbn_upload": "dry", "callsign": "N0CALL", "grid": "AA00", "enable_rtty": True}).rtty
