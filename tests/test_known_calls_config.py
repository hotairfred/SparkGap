"""Known-call prior config (itila_known_calls, itila_known_bonus): which calls earn the bonus,
and the 'rbn' list (the calls RBN heard lately) next to a file."""
import rbn_known
import sparkgap


class _Lib:
    """itila_set_known_calls as a recorder."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.bonus = 0.0

        def setter(buf: bytes, n: int, bonus: float) -> int:
            self.calls, self.bonus = buf[:n].decode().split("\n"), bonus
            return len(self.calls)

        self.itila_set_known_calls = setter


def test_short_calls_and_calls_that_start_a_longer_one_earn_no_bonus() -> None:
    got = sparkgap._prior_calls(["AI5I", "AI5IN", "W6T", "W6TI", "k1vut\n", "K1VUT", "N3F", "KD2RSI"])
    assert got == ["AI5IN", "K1VUT", "KD2RSI", "W6TI"]


def test_a_call_inside_another_but_not_at_its_start_keeps_its_bonus() -> None:
    assert sparkgap._prior_calls(["W1AW", "KW1AW"]) == ["KW1AW", "W1AW"]


def test_rbn_list(monkeypatch, tmp_path) -> None:
    lib = _Lib()
    seen = {}

    def refresh(d, own, days):
        seen.update(d=d, own=own, days=days)
        return {"AI5I", "AI5IN", "K1VUT", "N3F"}

    monkeypatch.setattr(sparkgap, "_get_itila_lib", lambda: lib)
    monkeypatch.setattr(rbn_known, "refresh", refresh)
    cfg = {"itila_known_calls": "rbn", "itila_known_bonus": 3, "rbn_known_dir": str(tmp_path),
           "callsign": "WF8Z", "rbn_known_days": 5}
    assert sparkgap._set_known_calls(cfg) == 2
    assert lib.calls == ["AI5IN", "K1VUT"] and lib.bonus == 3.0
    assert seen == {"d": tmp_path, "own": "WF8Z", "days": 5}


def test_file_list_and_off(monkeypatch, tmp_path) -> None:
    lib = _Lib()
    monkeypatch.setattr(sparkgap, "_get_itila_lib", lambda: lib)
    f = tmp_path / "known.txt"
    f.write_text("# heard lately\nK1VUT\nW1AW\n")
    assert sparkgap._set_known_calls({"itila_known_calls": str(f), "itila_known_bonus": 2}) == 2
    assert sparkgap._set_known_calls({"itila_known_calls": str(f), "itila_known_bonus": 0}) == 0
    assert sparkgap._set_known_calls({"itila_known_calls": str(tmp_path / "none.txt"),
                                      "itila_known_bonus": 2}) == 0
