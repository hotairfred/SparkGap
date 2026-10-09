"""Decoder library selection (cw_decoder config key)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

import sparkgap
from sparkgap import _ITILA_LIBS, _SCANNER_LIBS, _select_cw_decoder


@pytest.fixture(autouse=True)
def _reset_decoder_selection() -> Iterator[None]:
    """Reset the selected decoder to the default after each test, since
    _select_cw_decoder mutates a module global and test order must not matter."""
    yield
    _select_cw_decoder("itila")


def test_default_maps_to_freds_library() -> None:
    """Selecting "itila" points at Fred's original decoder library."""
    assert _select_cw_decoder("itila") == "./libitila.so"


def test_itila2_maps_to_second_library() -> None:
    """Selecting "itila2" points at the second decoder library."""
    assert _select_cw_decoder("itila2") == "./libitila2.so"


def test_unknown_name_rejected() -> None:
    """An unrecognized cw_decoder value raises with the config key named."""
    with pytest.raises(ValueError, match="cw_decoder"):
        _select_cw_decoder("bogus")


def test_every_listed_library_has_a_build_recipe() -> None:
    """Every library in _ITILA_LIBS has a matching Makefile build recipe."""
    makefile_text = (Path(sparkgap.__file__).resolve().parent / "Makefile").read_text()
    for lib_path in _ITILA_LIBS.values():
        basename = Path(lib_path).name
        assert basename in makefile_text


def test_each_decoder_selects_its_own_scanner() -> None:
    """cw_decoder alone picks the scanner: itila with Fred's, itila2 with itila2_scanner."""
    _select_cw_decoder("itila2")
    assert sparkgap._scanner_lib_path == "./libitila2_scanner.so"
    _select_cw_decoder("itila")
    assert sparkgap._scanner_lib_path == "./libitila_scanner.so"


def test_every_scanner_library_has_a_build_recipe() -> None:
    """Every library in _SCANNER_LIBS has a matching Makefile build recipe."""
    makefile_text = (Path(sparkgap.__file__).resolve().parent / "Makefile").read_text()
    for lib_path in _SCANNER_LIBS.values():
        assert f"{Path(lib_path).name}:" in makefile_text
