"""ITILA raw lines carry the copy's SNR as the spot rule gets it, so a replay of the log ranks copies
as the live node did (spot_rule2 gate 9); the fields sit after cost=, where every log reader allows them."""

import re

import sparkgap

LINE = "06:00:00 INFO ITILA raw 7028.0 kHz cost=1.00{}: 'CQ W9SN'"
SEAT = re.compile(r"ITILA raw ([\d.]+) kHz")                                  # seat.py
PARITY = re.compile(r"ITILA raw\s+(?P<freq>[\d.]+)\s+kHz.*'(?P<text>.*)'$")   # tools/eval/parity_score.py


def test_fields() -> None:
    assert sparkgap._raw_snr(18.44, 11.16) == " snr=18.4 rep=11.2"
    assert sparkgap._raw_snr(18.44, None) == " snr=18.4"
    assert sparkgap._raw_snr(18.44, float("nan")) == " snr=18.4"
    assert sparkgap._raw_snr(None, None) == sparkgap._raw_snr(float("nan"), 3.0) == ""


def test_other_readers_still_read_the_line() -> None:
    line = LINE.format(sparkgap._raw_snr(18.4, 11.2))
    assert SEAT.search(line)[1] == "7028.0"
    assert PARITY.search(line)["text"] == "CQ W9SN"
    assert line.split(": ", 1)[-1] == "'CQ W9SN'"                              # station/daily_check.py
