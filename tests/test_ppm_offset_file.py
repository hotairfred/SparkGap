"""ppm_offset_file: a calibration job writes the offset, SparkGap takes it without a restart."""

import os
import time

import sparkgap


def _sg(tmp_path, text=None, ppm=0.5):
    path = tmp_path / "ppm.txt"
    if text is not None:
        path.write_text(text)
    sg = sparkgap.SparkGap.__new__(sparkgap.SparkGap)
    sg.ppm_offset, sg._ppm_file, sg._ppm_mtime = ppm, str(path), None
    return sg, path


def test_file_sets_the_offset_and_a_change_is_taken(tmp_path) -> None:
    sg, path = _sg(tmp_path, "# from ppm_autocal\n+0.76\n")
    sg._read_ppm_file()
    assert sg.ppm_offset == 0.76
    assert abs(sg._corrected_freq_khz(14000.0) - 14000.0 * (1 - 0.76e-6)) < 1e-9
    path.write_text("0.31\n")
    os.utime(path, (time.time() + 5, time.time() + 5))
    sg._read_ppm_file()
    assert sg.ppm_offset == 0.31


def test_missing_garbled_or_wild_file_leaves_the_offset(tmp_path) -> None:
    sg, _ = _sg(tmp_path)                    # no file
    sg._read_ppm_file()
    assert sg.ppm_offset == 0.5
    for text in ("", "abc\n", "123\n"):      # empty, not a number, past PPM_FILE_MAX
        sg, _ = _sg(tmp_path, text)
        sg._read_ppm_file()
        assert sg.ppm_offset == 0.5, text
