"""sparkgap.sh names a receiver <radio>-<n>: the radio's name and its DAX-IQ channel."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "sparkgap.sh"
RADIO = {"type": "flex", "nickname": "Lake", "model": "FLEX-6600", "address": "192.0.2.101",
         "callsign": "N0CALL-2", "grid": "AA00aa", "max_slices": 4, "max_panadapters": 4,
         "daxiq_channels": 4}
TEMPLATE = {
    "sdr_type": "flex",
    "sdr_ip": "192.0.2.10",
    "callsign": "N0CALL",
    "bands": ["40m"],
    "telnet_port": 7300,
    "flex_udp_port": 4993,
}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    code = tmp_path / "code"
    code.mkdir()
    (code / "sparkgap.py").write_text("BANDS = dict(zip(['40m', '20m'], [1, 2]))\n")
    git = ["git", "-C", str(code), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "c"], check=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "config" / "template.json").write_text(json.dumps(TEMPLATE))
    (tmp_path / "config" / "Lake.radio.json").write_text(json.dumps(RADIO))
    (tmp_path / "instances").write_text("# name code-dir config python\n")
    return tmp_path


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {"SPARKGAP_ROOT": str(root), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)


def test_process_commands_go_through_the_server(root: Path) -> None:
    """No server running: start, stop and status say how to start it; nothing runs."""
    (root / "code" / "sparkgap_ctl.py").symlink_to(SCRIPT.parent.parent / "sparkgap_ctl.py")
    (root / "code" / "process_link.py").symlink_to(SCRIPT.parent.parent / "process_link.py")
    (root / "server.json").write_text(json.dumps({"port": 1, "join_code": "ACDEFH"}))
    for args in (["start", "Lake-1"], ["stop", "Lake-1"], ["status"]):
        r = run(root, *args)
        assert r.returncode == 2, (args, r.stdout, r.stderr)
        assert "sparkgap.sh start" in r.stderr
    assert not list((root / "logs").glob("*.pid"))


def test_only_setup_start_stop_and_status_are_taken(root: Path) -> None:
    for args in (["band", "Lake-1"], ["rec", "Lake-1"], ["rbn"], ["server"], ["server", "go"],
                 ["discover", "a", "b"], ["template", "192.0.2.101"], ["migrate"], ["setup", "x"],
                 ["status", "Lake-1"], ["restart"], ["add", "Lake-1", "40m"], ["update"],
                 ["remove", "Lake-1"]):
        assert run(root, *args).returncode == 2


def test_help_lists_only_setup_start_stop_and_status(root: Path) -> None:
    help_ = run(root, "help").stdout
    commands = help_.split("Commands\n")[1].split("\n\n")[0]
    assert [line.split()[0] for line in commands.splitlines()] == ["setup", "start", "stop",
                                                                   "status"]
    terms = help_.split("Key terms\n")[1].split("\n\n")[0]
    assert [line.split()[0] for line in terms.splitlines()] == ["radio", "station", "node",
                                                                "feeder", "receiver"]
    assert run(root, "start", "--help").stdout == help_
    assert "\033[" not in help_  # plain when not a terminal


def test_stop_remembers_the_running_receivers_for_start(root: Path) -> None:
    import os

    with (root / "instances").open("a") as f:
        f.write(f"Lake-1 {root}/Lake-1 Lake-1.local.json python3\n")
        f.write(f"Lake-2 {root}/Lake-2 Lake-2.local.json python3\n")
    (root / "logs" / "Lake-1.pid").write_text(str(os.getpid()))  # a live pid: Lake-1 runs
    r = run(root, "stop")
    assert r.returncode == 2 and "the server does not" in r.stderr  # no server in this test
    assert (root / "logs" / "running").read_text().split() == ["Lake-1"]
