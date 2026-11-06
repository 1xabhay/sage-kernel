"""`make preflight`: nothing goes live unless every check passes; each failure says why.

Fakes stand in for the world: HTTP is test_status's fake ``urlopen``, commands
(``make -C robot check``, ``docker context show``) answer from a table, and the
journal backups are files in a temporary folder.
"""

import io
import os
import time

import pytest
from test_status import ENV, UP, fake

from reachy_sage_voice.preflight import main

NOW = time.time()
ROBOT_OK = "journal: persistent\nactive\nenabled\n{...}\nguard: loaded\nenabled\nguard: at boot\n"
OLLAMA = "http://127.0.0.1:11434/api/ps"


def runner(robot=(0, ROBOT_OK), docker=(0, "colima\n")):
    def run(command):
        return robot if command[0] == "make" else docker
    return run


@pytest.fixture
def backups(tmp_path):
    copy = tmp_path / "journal-20261022-1600.sqlite3"
    copy.write_bytes(b"")
    copy.chmod(0o600)
    os.utime(copy, (NOW - 600, NOW - 600))
    return tmp_path


def preflight(backups, answers=UP, run=None):
    out = io.StringIO()
    env = {**ENV, "SAGE_KERNEL_BACKUPS": str(backups)}
    code = main(env, fake(answers), run or runner(), lambda: NOW, out)
    lines = {line.split()[0]: line for line in out.getvalue().splitlines()}
    return code, lines


def failed(lines):
    return {name for name, line in lines.items() if line.split()[1] == "FAIL"}


def test_all_checks_pass_exits_zero(backups):
    code, lines = preflight(backups)
    assert code == 0
    assert set(lines) == {"status", "robot", "docker", "backup", "ollama"}
    assert failed(lines) == set()


def test_a_service_down_fails_status_and_names_it(backups):
    code, lines = preflight(backups, {**UP, "http://127.0.0.1:8782/health": {"ready": False}})
    assert code == 1
    assert failed(lines) == {"status"} and "tts" in lines["status"]


def test_a_failing_robot_check_fails_with_its_last_line(backups):
    code, lines = preflight(backups, run=runner(robot=(255, "ssh: connect to host: timed out\n")))
    assert code == 1
    assert failed(lines) == {"robot"} and "timed out" in lines["robot"]


def test_a_robot_check_missing_a_line_fails_even_if_it_exits_zero(backups):
    # robot/Makefile's `grep -q ... && echo` steps fail silently under set -e.
    code, lines = preflight(backups, run=runner(robot=(0, ROBOT_OK.replace("guard: loaded\n", ""))))
    assert code == 1
    assert failed(lines) == {"robot"} and "guard: loaded" in lines["robot"]


def test_a_docker_context_other_than_colima_fails(backups):
    code, lines = preflight(backups, run=runner(docker=(0, "desktop-linux\n")))
    assert code == 1
    assert failed(lines) == {"docker"} and "desktop-linux" in lines["docker"]


def test_no_backup_fails_and_says_how_to_make_one(tmp_path):
    code, lines = preflight(tmp_path)
    assert code == 1
    assert failed(lines) == {"backup"} and "make backup-journal" in lines["backup"]


def test_a_backup_older_than_an_hour_fails(backups):
    copy = next(backups.glob("*.sqlite3"))
    os.utime(copy, (NOW - 3700, NOW - 3700))
    code, lines = preflight(backups)
    assert code == 1
    assert failed(lines) == {"backup"} and "62 min old" in lines["backup"]


def test_a_backup_others_can_read_fails(backups):
    (backups / "journal-20261022-1600.sqlite3-wal").write_bytes(b"")
    (backups / "journal-20261022-1600.sqlite3-wal").chmod(0o644)
    code, lines = preflight(backups)
    assert code == 1
    assert failed(lines) == {"backup"} and "0o644" in lines["backup"]


def test_two_models_loaded_in_ollama_fails(backups):
    two = {"models": [{"name": "qwen3:4b-instruct"}, {"name": "qwen2.5vl:3b"}]}
    code, lines = preflight(backups, {**UP, OLLAMA: two})
    assert code == 1
    assert failed(lines) == {"ollama"} and "qwen2.5vl:3b" in lines["ollama"]
