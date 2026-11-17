"""`make restore-drill` and `make restore`: a journal backup is only worth what restores from it.

The drill never touches the live kernel: it copies a backup into a temp folder, starts a throwaway
kernel on it (a free port, the conformance suite's fake model) and has it answer one new line.
The restore puts a backup into the live volume while the kernel is stopped. The journal it
replaces is moved aside inside the volume, never deleted.
"""

import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KERNEL, VOLUME = "sage-kernel-kernel-1", "sage-kernel_kernel-data"
DOCKER = ("docker", "--context", "colima")  # named: the default context has fallen back before (2026-10-20)

# Runs inside the kernel's image, as its user, on the volume: argv is the data folder and a stamp, stdin the
# backup. The old journal and its sqlite side files are renamed, the new one is written beside and renamed in.
INSIDE = """
import os, sqlite3, sys
data, stamp = sys.argv[1], sys.argv[2]
new = os.path.join(data, "kernel.sqlite3.restoring")
with open(new, "wb") as file:
    file.write(sys.stdin.buffer.read())
    file.flush()
    os.fsync(file.fileno())
for name in ("kernel.sqlite3", "kernel.sqlite3-wal", "kernel.sqlite3-shm"):
    path = os.path.join(data, name)
    if os.path.exists(path):
        os.rename(path, f"{path}.before-restore-{stamp}")
os.rename(new, os.path.join(data, "kernel.sqlite3"))
rows, last = sqlite3.connect(f"file:{os.path.join(data, 'kernel.sqlite3')}?immutable=1", uri=True).execute(
    "select count(*), max(sequence) from journal").fetchone()
print(f"{rows} rows to sequence {last}")
"""


def check(backup: Path) -> tuple[int, int]:
    """A backup's row count and last sequence; exits, naming it, when it is no whole journal."""
    try:
        with sqlite3.connect(f"file:{backup}?immutable=1", uri=True) as journal:
            if journal.execute("pragma integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("integrity check failed")
            rows, last = journal.execute("select count(*), max(sequence) from journal").fetchone()
    except sqlite3.Error as error:
        sys.exit(f"restore: {backup.name} is not a whole journal ({error})")
    if not rows:
        sys.exit(f"restore: {backup.name} holds no journal rows")
    return rows, last


def newest(folder: Path) -> Path:
    """The newest journal backup `make backup` left in `folder` (their names sort by time)."""
    found = sorted(folder.glob("journal-*.sqlite3"))
    if not found:
        sys.exit(f"restore: no journal-*.sqlite3 in {folder}. Run make backup first.")
    return found[-1]


def drill(backup: Path) -> str:
    """Prove `backup` restores: a throwaway kernel opens a copy of it and answers a new line."""
    sys.path.insert(0, str(ROOT))
    from conformance.test_conformance import TOKENS, KernelProcess, Provider
    rows, last = check(backup)
    provider, turn = Provider(), f"drill-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory() as state:
        shutil.copyfile(backup, Path(state) / "kernel.sqlite3")
        kernel = KernelProcess(state, provider)
        try:
            kernel.start()  # it reads the whole journal back here, and exits if it cannot
            status, hello = kernel.device("POST", "/v1/hello", {"protocols": [1], "manifest": {"senses": [], "tools": []}},
                                          TOKENS["phone"])
            if status != 200 or hello["sequence"] < last:
                sys.exit(f"restore: a kernel on {backup.name} stands at {hello}, behind the backup's sequence {last}")
            kernel.device("POST", "/v1/events", {"kind": "submitted", "turn_id": turn, "text": "Are you there?",
                                                 "input": {"medium": "text"}}, TOKENS["phone"])
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                effects = kernel.device("GET", f"/v1/effects?since={hello['sequence']}&wait=2", token=TOKENS["phone"])[1]
                if any(e["kind"] == "reply" and e.get("turn_id") == turn for e in (effects or {}).get("effects", ())):
                    break
            else:
                sys.exit(f"restore: a kernel on {backup.name} did not answer a new line")
        except RuntimeError as error:
            sys.exit(f"restore: no kernel starts on {backup.name}: {str(error)[:300]}")
        finally:
            kernel.stop()
            provider.server.shutdown()
    return (f"drill: ok. {backup.name}: {rows} rows to sequence {last}; a throwaway kernel opened it and "
            "answered a new line.")


def restore(run, backup: Path, stamp: str) -> str:
    """Put `backup` into the live volume, the kernel stopped; the journal it replaces is moved aside."""
    rows, last = check(backup)
    if run([*DOCKER, "inspect", "-f", "{{.State.Running}}", KERNEL], check=False).stdout.strip() == "true":
        sys.exit("restore: the kernel is running. Run make sage-down first: it also backs up the journal as it is now.")
    image = run([*DOCKER, "inspect", "-f", "{{.Config.Image}}", KERNEL]).stdout.strip()
    done = run([*DOCKER, "run", "--rm", "-i", "--network", "none", "-v", f"{VOLUME}:/data", "--entrypoint", "python3",
                image, "-c", INSIDE, "/data", stamp], stdin=backup)
    aside = f"/data/kernel.sqlite3.before-restore-{stamp}"
    if done.stdout.strip() != f"{rows} rows to sequence {last}":
        sys.exit(f"restore: the volume reads back as {done.stdout.strip()!r}, not the backup's {rows} rows to "
                 f"sequence {last}. The journal from before is in the volume as {aside}.")
    return (f"restore: {backup.name} is the live journal now ({rows} rows to sequence {last}). The journal from "
            f"before is kept in the volume as {aside}. make sage-up starts Sage on it.")


def _run(command: list[str], check: bool = True, stdin: Path | None = None) -> subprocess.CompletedProcess:
    with (stdin.open("rb") if stdin else open("/dev/null", "rb")) as stream:
        done = subprocess.run(command, stdin=stream, capture_output=True, check=False)
    done.stdout, done.stderr = done.stdout.decode(), done.stderr.decode()
    if check and done.returncode:
        sys.exit(f"restore: {' '.join(command[:6])} ...: {done.stderr.strip() or done.returncode}")
    return done


if __name__ == "__main__":
    if sys.argv[1:2] == ["drill"] and len(sys.argv) <= 3:
        print(drill(Path(sys.argv[2]) if sys.argv[2:] else newest(Path(".backups"))))
    elif sys.argv[1:2] == ["restore"] and len(sys.argv) == 3:
        print(restore(_run, Path(sys.argv[2]), time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())))
    else:
        sys.exit("usage: restore.py drill [backup] | restore <backup>")
