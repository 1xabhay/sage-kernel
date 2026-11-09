"""``make preflight``: safe to go live? One line each, with why. Run it before anything live.

    status   every ``make status`` check is up (status.py)
    robot    ``make -C robot check`` exits 0 and reports journal, guard and guard-at-boot
    docker   the Docker context is colima
    backup   the newest kernel journal backup in SAGE_KERNEL_BACKUPS (~/sage-kernel/.backups)
             is under an hour old and mode 600, with its -wal and -shm (make backup-journal)
    ollama   at most one model loaded, so nothing else holds the GPU

Exits 1 if any fails. Stdlib only, like status.py.
"""

import io
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from typing import TextIO

from reachy_sage_voice import status

APP = pathlib.Path(__file__).resolve().parents[1]
MAX_AGE_S = 3600
# What robot/Makefile's check prints when all is well. Its `grep -q ... && echo` steps fail
# without failing the check, so a missing line is the failure.
ROBOT_LINES = ("journal: persistent", "guard: loaded", "guard: at boot")


def run_command(command: list[str]) -> tuple[int, str]:
    """Run ``command``; return its exit code and its output, stderr included."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)
    return done.returncode, done.stdout + done.stderr


def _status(env: Mapping[str, str], get: Callable, motion: pathlib.Path) -> tuple[bool, str]:
    out = io.StringIO()
    if status.main(env, get, out, motion) == 0:
        return True, "all up"
    down = [line.split()[0] for line in out.getvalue().splitlines() if line.split()[1] == "down"]
    return False, f"down: {', '.join(down)} (make status says why)"


def _robot(run: Callable) -> tuple[bool, str]:
    code, output = run(["make", "-s", "-C", str(APP / "robot"), "check"])
    if code != 0:
        last = output.strip().splitlines()[-1:] or [f"exit {code}"]
        return False, f"make -C robot check failed: {last[0]}"
    missing = [line for line in ROBOT_LINES if line not in output]
    if missing:
        return False, f"make -C robot check did not report: {', '.join(missing)}"
    return True, ", ".join(ROBOT_LINES)


def _docker(run: Callable) -> tuple[bool, str]:
    code, output = run(["docker", "context", "show"])
    name = output.strip()
    return (True, name) if code == 0 and name == "colima" else (
        False, f"context is {name or 'unknown'}, not colima")


def _backup(folder: pathlib.Path, now: float) -> tuple[bool, str]:
    copies = sorted(folder.glob("*.sqlite3"), key=lambda path: path.stat().st_mtime)
    if not copies:
        return False, f"no journal backup in {folder} (make backup-journal)"
    newest = copies[-1]
    minutes = (now - newest.stat().st_mtime) / 60
    if minutes * 60 > MAX_AGE_S:
        return False, f"newest, {newest.name}, is {minutes:.0f} min old (make backup-journal)"
    for path in folder.glob(newest.name + "*"):  # with its -wal and -shm
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            return False, f"{path.name} is mode {oct(mode)}, not 0o600"
    return True, f"{newest.name}, {minutes:.0f} min old"


def _one_model(answer: dict) -> tuple[bool, str]:
    names = [model["name"] for model in answer.get("models", [])]
    why = "loaded: " + ", ".join(names) if names else "no model loaded"
    return len(names) <= 1, why


def main(env: Mapping[str, str] = os.environ, get: Callable = urllib.request.urlopen,
         run: Callable = run_command, now: Callable[[], float] = time.time,
         out: TextIO = sys.stdout, motion: pathlib.Path = status.MOTION_FILE) -> int:
    """Print each check; return 1 if any fails. ``motion``: the running voice's heartbeat."""
    backups = env.get("SAGE_KERNEL_BACKUPS") or str(pathlib.Path.home() / "sage-kernel/.backups")
    results = [
        ("status", _status(env, get, motion)),
        ("robot", _robot(run)),
        ("docker", _docker(run)),
        ("backup", _backup(pathlib.Path(backups), now())),
        ("ollama", status.check(status.OLLAMA, _one_model, get)),
    ]
    for name, (ok, why) in results:
        print(f"{name:<8} {'ok' if ok else 'FAIL':<5} {why}", file=out)
    return 0 if all(ok for _, (ok, _) in results) else 1


if __name__ == "__main__":
    sys.exit(main())
