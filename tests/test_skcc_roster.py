"""SKCC member calls (skcc_roster.py): roster parsing, daily refresh, offline cache."""

from pathlib import Path

from skcc_roster import member_calls, refresh

ROSTER = ("SKCCNR|CALL|NAME|QTH|SPC|OLDCALL|SKCC|DXCODE|JOINDATE|CENTDATE|TRIBDATE|TX8DATE|"
          "SENDATE|DXENTITY\n"
          "1|KC9ECI|Tom|Galesville|WI|M0KCE|1|291|02 Jan 2006|||||USA\n"
          "2|ve3/k1abc|Al|x|ON||2|1||||||Canada\n")


def test_member_calls_are_current_calls_not_old_ones() -> None:
    assert member_calls(ROSTER) == {"KC9ECI", "K1ABC"}


def test_refresh_daily_and_offline(tmp_path: Path) -> None:
    fetches: list[int] = []

    def fetch() -> bytes:
        fetches.append(1)
        return ROSTER.encode()

    assert refresh(tmp_path, fetch=fetch) == {"KC9ECI", "K1ABC"}
    assert refresh(tmp_path, fetch=fetch) == {"KC9ECI", "K1ABC"}
    assert len(fetches) == 1                       # fresh cache: no second download

    def offline() -> bytes:
        raise OSError("network unreachable")

    assert refresh(tmp_path, fetch=offline, now=lambda: 2e10) == {"KC9ECI", "K1ABC"}

    def truncated() -> bytes:
        import http.client
        raise http.client.IncompleteRead(b"SKCCNR|")

    assert refresh(tmp_path, fetch=truncated, now=lambda: 2e10) == {"KC9ECI", "K1ABC"}


def test_refresh_rejects_a_page_that_is_not_the_roster(tmp_path: Path) -> None:
    assert refresh(tmp_path, fetch=lambda: b"<html>maintenance</html>") == set()
