"""tools/sim/gen_dense.py: a small dense scene has every runner, no call twice, and one I/Q file."""
import json
import subprocess
import sys
import wave
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_a_small_dense_scene(tmp_path: Path) -> None:
    subprocess.run([sys.executable, str(ROOT / "tools/sim/gen_dense.py"), str(tmp_path), "4",
                    "--minutes", "0.5", "--group", "2", "--procs", "2", "--compose-procs", "2",
                    "--lo", "7010", "--hi", "7020"], check=True, capture_output=True)
    truth = json.loads((tmp_path / "dense.truth.json").read_text())
    runners = [s for s in truth["stations"] if s["role"] == "runner"]
    assert len(runners) == 4 and len({s["cell"] for s in runners}) == 4     # cells renumbered across groups
    calls = Counter(s["call"] for s in truth["stations"])
    assert max(calls.values()) == 1                                       # no call twice on the band
    freqs = sorted(s["rf_hz"] for s in runners)
    assert all(b - a >= 99 for a, b in zip(freqs, freqs[1:]))              # 100 Hz apart (cellgen's pitch rounding)
    with wave.open(str(tmp_path / "dense_7030kHz_192000_24bit.wav")) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (2, 3, 192000)
        assert abs(w.getnframes() / 192000 - 30) < 0.1
    assert not list(tmp_path.glob("group*_7030kHz_192000_24bit.wav"))      # the group files are summed and gone
