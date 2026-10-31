"""Bin liveness counts more calls as known: a portable call by its base call, and calls on the
seat's spot_validation_calls list (rbn, rbn+skcc) beside MASTER.SCP and add_calls."""

import pytest

import sparkgap

SCP = {"W1AW", "K1ABC"}


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A seat list holding N0RBN (heard on RBN, not in MASTER.SCP)."""
    monkeypatch.setattr(sparkgap, "_liveness_listed", lambda c: c.split("/")[0] in {"N0RBN"})


def test_a_portable_call_counts_by_its_base_call() -> None:
    assert sparkgap._copies_known_runner("CQ CQ DE W1AW/8 W1AW/8 K", SCP)


@pytest.mark.usefixtures("listed")
def test_a_call_on_the_seat_list_counts() -> None:
    assert sparkgap._copies_known_runner("CQ CQ DE N0RBN N0RBN K", SCP)


@pytest.mark.usefixtures("listed")
def test_a_call_on_no_list_does_not_count() -> None:
    assert not sparkgap._copies_known_runner("CQ CQ DE N9ZZZ N9ZZZ K", SCP)


def test_without_a_seat_list_only_scp_counts() -> None:
    assert not sparkgap._copies_known_runner("CQ CQ DE N0RBN N0RBN K", SCP)


def test_the_list_is_read_only_with_liveness_and_a_list_set() -> None:
    assert sparkgap._liveness_list({"spot_validation_calls": "rbn"}, object()) is None
    assert sparkgap._liveness_list({"itila2_bin_liveness": True}, object()) is None
