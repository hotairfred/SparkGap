"""downloads.py: fetch when due, keep what checks, keep the previous copy on failure, note it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import downloads

URL = "https://example.invalid/list.txt"


def _lines(path: Path) -> int:
    text = path.read_text()
    if not text.startswith("LIST\n"):
        raise ValueError("not a list")
    return len(text.splitlines()) - 1


def _refresh(path: Path, data: bytes | Exception, **kw: object) -> tuple[bool, list[int]]:
    calls: list[int] = []

    def get() -> bytes:
        calls.append(1)
        if isinstance(data, Exception):
            raise data
        return data

    return downloads.refresh(path, URL, _lines, "lines", "test", get=get, **kw), calls  # type: ignore[arg-type]


def _entry(path: Path) -> dict[str, object]:
    return json.loads((path.parent / downloads.MANIFEST).read_text())[path.name]


def test_a_missing_file_is_downloaded_checked_and_noted(tmp_path: Path) -> None:
    path = tmp_path / "list.txt"
    ok, calls = _refresh(path, b"LIST\nA\nB\n")
    assert ok and calls == [1] and path.read_text() == "LIST\nA\nB\n"
    e = _entry(path)
    assert (e["name"], e["path"], e["url"], e["records"], e["what"], e["error"]) == (
        "list.txt", str(path.resolve()), URL, 2, "lines", None)
    assert e["downloaded"] == path.stat().st_mtime
    assert not list(tmp_path.glob("*.tmp"))


def test_a_fresh_file_is_not_fetched_and_an_old_one_is(tmp_path: Path) -> None:
    path = tmp_path / "list.txt"
    _refresh(path, b"LIST\nA\n")
    _, calls = _refresh(path, b"LIST\nA\nB\n", max_age_s=3600.0)
    assert calls == [] and _entry(path)["records"] == 1
    _, calls = _refresh(path, b"LIST\nA\nB\n", max_age_s=3600.0, now=lambda: 2e10)
    assert calls == [1] and _entry(path)["records"] == 2


@pytest.mark.parametrize("bad", [OSError("timed out"), b"<html>maintenance</html>"])
def test_a_failed_fetch_keeps_the_previous_copy_and_says_why(
        tmp_path: Path, bad: bytes | Exception) -> None:
    path = tmp_path / "list.txt"
    _refresh(path, b"LIST\nA\n")
    ok, _ = _refresh(path, bad, max_age_s=0.0, now=lambda: 2e10)
    assert ok and path.read_text() == "LIST\nA\n"
    e = _entry(path)
    assert e["records"] == 1 and e["error"] and e["checked"] == 2e10
    assert not list(tmp_path.glob("*.tmp"))


def test_an_empty_download_never_replaces_a_copy_whatever_the_check(tmp_path: Path) -> None:
    path = tmp_path / "list.txt"
    path.write_text("LIST\nA\n")
    ok = downloads.refresh(path, URL, lambda p: 0, "lines", "test", get=lambda: b"",
                           current=lambda p: False)
    assert ok and path.read_text() == "LIST\nA\n" and "empty" in str(_entry(path)["error"])


def test_no_copy_and_no_download_is_no_file(tmp_path: Path) -> None:
    path = tmp_path / "list.txt"
    ok, _ = _refresh(path, OSError("offline"))
    assert not ok and not path.exists()
    e = _entry(path)
    assert e["records"] is None and e["downloaded"] is None and "offline" in str(e["error"])


def test_a_copy_no_longer_current_is_fetched_again(tmp_path: Path) -> None:
    path = tmp_path / "list.txt"
    _refresh(path, b"LIST\nA\n")
    _, calls = _refresh(path, b"LIST\nA\nB\n", current=lambda p: "B" in p.read_text())
    assert calls == [1] and path.read_text() == "LIST\nA\nB\n"
    _, calls = _refresh(path, b"LIST\n", current=lambda p: "B" in p.read_text())
    assert calls == []


def test_a_copy_that_cannot_be_read_is_a_failed_refresh_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "list.txt"
    path.mkdir()                                     # not a file: current() cannot read it
    ok, calls = _refresh(path, b"LIST\n", current=lambda p: p.read_bytes() == b"")
    assert ok and calls == [1] and _entry(path)["error"]


def test_a_damaged_manifest_entry_is_left_out(tmp_path: Path) -> None:
    _refresh(tmp_path / "list.txt", b"LIST\nA\n")
    notes = json.loads((tmp_path / downloads.MANIFEST).read_text())
    notes["bad.txt"] = {**notes["list.txt"], "name": "bad.txt", "records": "many"}
    notes["junk"] = 17
    (tmp_path / downloads.MANIFEST).write_text(json.dumps(notes))
    assert [e["name"] for e in downloads.listed([tmp_path])] == ["list.txt"]


def test_listed_reads_each_folder_once(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    _refresh(tmp_path / "a" / "one.txt", b"LIST\nA\n")
    _refresh(tmp_path / "two.txt", b"LIST\n")
    names = [e["name"] for e in downloads.listed([tmp_path, tmp_path / "a", tmp_path / "a" / ".."])]
    assert names == ["two.txt", "one.txt"]
    assert downloads.listed([tmp_path / "nowhere"]) == []


def test_server_date_reads_last_modified(monkeypatch: pytest.MonkeyPatch) -> None:
    class Resp:
        headers = {"Last-Modified": "Fri, 13 Nov 2026 18:26:35 GMT"}

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    seen: list[str] = []
    monkeypatch.setattr(downloads.urllib.request, "urlopen",
                        lambda req, timeout: seen.append(req.get_method()) or Resp())
    assert downloads.server_date(URL, "test") == 1_794_594_395.0
    assert seen == ["HEAD"]


def test_a_folder_that_cannot_be_made_is_no_download_not_a_crash(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")              # a file where the folder should be
    path = tmp_path / "file" / "sub" / "list.txt"
    ok, calls = _refresh(path, b"LIST\nA\n")
    assert not ok and calls == []
