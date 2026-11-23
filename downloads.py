"""Files SparkGap downloads (SKCC roster, RBN archive days and reference files, RBN auth library).

refresh() fetches a file when it is missing, too old or no longer current, and keeps a download
only when its check passes; a failed fetch keeps the previous copy. Each refresh is noted in
downloads.json beside the file (path, URL, record count, when downloaded, the last failure), which
the server reads so SparkGap Web and sparkgap.sh status show what this machine holds.
"""

from __future__ import annotations

import contextlib
import email.utils
import fcntl
import http.client
import json
import logging
import os
import sqlite3
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

MANIFEST = "downloads.json"
FETCH_TIMEOUT_S = 60.0

log = logging.getLogger("sparkgap")

Check = Callable[[Path], int | None]  # a file's record count; raises when it is not that file
# a fetch offline, refused or cut short (URLError is an OSError), or a check refusing the file
FAILED = (OSError, http.client.HTTPException, ValueError, sqlite3.Error)


def fetch(url: str, user_agent: str, timeout_s: float = FETCH_TIMEOUT_S) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read()
    if not data:
        raise ValueError("empty download")  # never let a zero-length reply replace a copy
    return data


def server_date(url: str, user_agent: str, timeout_s: float = FETCH_TIMEOUT_S) -> float | None:
    """When url's file last changed on its server (Last-Modified), without downloading it."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        stamp = resp.headers.get("Last-Modified")
    if not stamp:
        return None
    try:
        return email.utils.parsedate_to_datetime(stamp).timestamp()
    except (TypeError, ValueError):
        return None


def write_atomic(path: Path, data: bytes, check: Check | None = None) -> int | None:
    """Write through a temp file of this writer's own and keep it only when check passes: a crash,
    a short download or a wrong file never replaces the copy, and copies sharing it never collide.
    Returns check's record count."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        records = check(tmp) if check else None
        os.chmod(tmp, 0o644)
        tmp.replace(path)
        return records
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def _locked(folder: Path) -> Iterator[None]:
    """One writer of a folder's downloads.json at a time (every copy on the machine shares it)."""
    with open(folder / f"{MANIFEST}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read(folder: Path) -> dict[str, dict[str, Any]]:
    try:
        notes = json.loads((folder / MANIFEST).read_text())
    except (OSError, ValueError):
        return {}
    return notes if isinstance(notes, dict) else {}


def note(path: Path, url: str, records: int | None, what: str, error: str | None,
         now: float | None = None) -> None:
    """Record a refresh of path in its folder's downloads.json, keyed by file name."""
    path = path.resolve()
    when = time.time() if now is None else now
    entry = {
        "name": path.name,
        "path": str(path),
        "url": url,
        "records": records,
        "what": what,
        "downloaded": path.stat().st_mtime if path.exists() else None,
        "checked": when,
        "error": error,
    }
    try:
        with _locked(path.parent):
            notes = _read(path.parent)
            notes[path.name] = entry
            write_atomic(path.parent / MANIFEST, json.dumps(notes, indent=1).encode())
    except OSError as e:
        log.warning("downloads: cannot note %s (%r)", path, e)


def listed(folders: Iterable[Path]) -> list[dict[str, Any]]:
    """Every noted download in these folders, each folder once, by file name."""
    seen: set[Path] = set()
    out: list[dict[str, Any]] = []
    for folder in folders:
        folder = folder.resolve()
        if folder in seen:
            continue
        seen.add(folder)
        out += [e for _, v in sorted(_read(folder).items()) if (e := _entry(v))]
    return out


def _holds(current: Callable[[Path], bool], path: Path) -> bool:
    """current(path), False when the copy cannot be read (a directory, no permission)."""
    try:
        return current(path)
    except FAILED:
        return False


def _entry(e: Any) -> dict[str, Any] | None:
    """A noted download with the types its readers expect, or None: a damaged
    downloads.json never stops a status from showing."""
    if not isinstance(e, dict):
        return None
    num = (int, float)
    if not (isinstance(e.get("name"), str) and isinstance(e.get("path"), str)
            and (e.get("records") is None or type(e["records"]) is int)
            and all(e.get(k) is None or (isinstance(e[k], num) and not isinstance(e[k], bool))
                    for k in ("downloaded", "checked"))
            and all(e.get(k) is None or isinstance(e[k], str) for k in ("url", "what", "error"))):
        return None
    return e


def refresh(path: Path, url: str, check: Check, what: str, user_agent: str,
            max_age_s: float | None = None, current: Callable[[Path], bool] | None = None,
            get: Callable[[], bytes] | None = None, now: Callable[[], float] = time.time) -> bool:
    """Download url to path when path is missing, older than max_age_s, or current(path) is False;
    keep the download only when check passes. A failed fetch keeps the previous copy. Notes the
    result in downloads.json and returns whether path exists; a folder that can't be made is no
    download and no copy, so the caller falls back to its own."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("downloads: %s cannot be made (%s): no download of %s", path.parent, e, url)
        return False
    due = (not path.exists()
           or (max_age_s is not None and now() - path.stat().st_mtime > max_age_s)
           or (current is not None and not _holds(current, path)))
    error = None
    records = None
    if due:
        try:
            data = (get or (lambda: fetch(url, user_agent)))()
            if not data:
                raise ValueError("empty download")
            records = write_atomic(path, data, check)
            log.info("downloads: %s from %s", path, url)
        except FAILED as e:                      # offline, refused, truncated or the wrong file
            error = f"{type(e).__name__}: {e}"
            log.warning("downloads: no download of %s (%s); %s", url, error,
                        "using the previous copy" if path.exists() else "no copy")
    if path.exists() and (not due or error):
        try:
            records = check(path)
        except FAILED as e:                      # the copy itself no longer reads
            error = error or f"{type(e).__name__}: {e}"
    note(path, url, records, what, error, now())
    return path.exists()
