"""Where SparkGap looks for decode_ft8 (Karlis Goba's ft8_lib)."""

import sparkgap


def test_config_path_wins() -> None:
    assert sparkgap._ft8_decoder({"ft8_decoder": "/opt/ft8/decode_ft8"}) == "/opt/ft8/decode_ft8"


def test_built_copy_beside_sparkgap_then_the_old_path(monkeypatch) -> None:
    monkeypatch.setattr(sparkgap.os.path, "exists", lambda p: p == sparkgap.FT8_DECODER_BUILT)
    assert sparkgap._ft8_decoder({}) == sparkgap.FT8_DECODER_BUILT
    monkeypatch.setattr(sparkgap.os.path, "exists", lambda p: False)
    assert sparkgap._ft8_decoder({}) == "/home/sparkgap/decode_ft8"
