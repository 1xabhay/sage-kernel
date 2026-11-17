"""make restore-drill and make restore: a journal backup is only worth what restores from it.

Host-only (make source): the image ships without make/. The drill runs for real, on a journal a
throwaway kernel wrote. The restore's file moves run for real in a temp folder; its Docker
commands go to a fake runner. Nothing here touches the live kernel or its volume.
"""

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "Makefile").exists()  # the checkout, not the image
if SOURCE:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "make"))
    import restore

    from conformance.test_conformance import KernelProcess, Provider


def written_journal(folder: Path) -> Path:
    """A journal as a kernel leaves it: one line said and answered."""
    kernel = KernelProcess(str(folder), Provider()).start()
    try:
        hello = {"protocols": [1], "manifest": {"senses": ["speech"], "tools": []}}
        kernel.device("POST", "/v1/hello", hello)
        kernel.device("POST", "/v1/events", {"kind": "submitted", "turn_id": "before-1", "text": "Is it raining?"})
        for _ in range(200):
            if kernel.device("GET", "/v1/effects?since=0&wait=1")[1]["effects"]:
                break
    finally:
        kernel.stop()
    return folder / "kernel.sqlite3"


class Docker:
    """The kernel container as the commands see it, and everything that was run."""

    def __init__(self, running=False, reads_back="2 rows to sequence 2"):
        self.running, self.reads_back, self.ran = running, reads_back, []

    def __call__(self, command, check=True, stdin=None):
        self.ran.append(" ".join(command))
        if "{{.State.Running}}" in command:
            return SimpleNamespace(returncode=0, stdout="true" if self.running else "false")
        if "{{.Config.Image}}" in command:
            return SimpleNamespace(returncode=0, stdout="sage-kernel:abc123\n")
        return SimpleNamespace(returncode=0, stdout=self.reads_back + "\n")


@unittest.skipUnless(SOURCE, "checks the source tree; the image ships without make/")
class Restore(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def backup(self) -> Path:
        state = self.folder / "state"
        state.mkdir()
        return written_journal(state)

    def test_the_drill_opens_a_backup_in_a_throwaway_kernel_and_has_it_answer_a_new_line(self):
        backup = self.backup()
        before = backup.read_bytes()
        said = restore.drill(backup)
        rows, last = restore.check(backup)
        self.assertGreater(rows, 0)
        self.assertRegex(said, rf"drill: ok\. .*{rows} rows to sequence {last}")
        self.assertEqual(backup.read_bytes(), before)  # the backup itself is never opened for writing
        self.assertNotIn("raining", said)  # counts only: what was said stays in the journal

    def test_a_backup_that_is_cut_short_empty_or_no_journal_fails_the_drill_by_name(self):
        whole = self.backup().read_bytes()
        cut, empty, other = (self.folder / name for name in ("cut.sqlite3", "empty.sqlite3", "other.sqlite3"))
        cut.write_bytes(whole[:len(whole) // 2])
        empty.write_bytes(b"")
        sqlite3.connect(other).executescript("create table notes (line text); insert into notes values ('x')")
        for bad in (cut, empty, other, self.folder / "missing.sqlite3"):
            with self.subTest(bad.name), self.assertRaisesRegex(SystemExit, bad.name):
                restore.drill(bad)

    def test_the_newest_backup_is_the_one_drilled_when_none_is_named(self):
        for name in ("journal-20261101T000000Z.sqlite3", "journal-20261116T000000Z.sqlite3", "stream-9.sqlite3"):
            (self.folder / name).write_bytes(b"x")
        self.assertEqual(restore.newest(self.folder).name, "journal-20261116T000000Z.sqlite3")
        with self.assertRaisesRegex(SystemExit, "make backup"):
            restore.newest(self.folder / "none")

    def test_a_restore_is_refused_while_the_kernel_runs_and_for_a_bad_backup_and_touches_nothing(self):
        backup = self.backup()
        docker = Docker(running=True)
        with self.assertRaisesRegex(SystemExit, "sage-down"):
            restore.restore(docker, backup, "20261116T120000Z")
        self.assertFalse([line for line in docker.ran if " run " in line])
        docker = Docker()
        (self.folder / "bad.sqlite3").write_bytes(b"not a journal")
        with self.assertRaisesRegex(SystemExit, "bad.sqlite3"):
            restore.restore(docker, self.folder / "bad.sqlite3", "20261116T120000Z")
        self.assertEqual(docker.ran, [])

    def test_a_restore_runs_in_the_kernel_s_own_image_on_its_volume_with_no_network_and_removes_no_volume(self):
        backup = self.backup()
        docker = Docker(reads_back="{} rows to sequence {}".format(*restore.check(backup)))
        self.assertIn("is the live journal now", restore.restore(docker, backup, "20261116T120000Z"))
        (ran,) = [line for line in docker.ran if " run " in line]
        self.assertRegex(ran, r"^docker --context colima run --rm -i --network none "
                              r"-v sage-kernel_kernel-data:/data --entrypoint python3 sage-kernel:abc123 -c ")
        self.assertFalse([line for line in docker.ran if "volume" in line or " rm " in line or "prune" in line])

    def test_the_journal_a_restore_replaces_is_moved_aside_whole_and_never_deleted(self):
        backup = self.backup()
        data = self.folder / "data"
        data.mkdir()
        for name, content in {"kernel.sqlite3": b"the journal before", "kernel.sqlite3-wal": b"its log",
                              "devices": b"tokens"}.items():
            (data / name).write_bytes(content)
        with backup.open("rb") as stream:
            done = subprocess.run([sys.executable, "-c", restore.INSIDE, str(data), "20261116T120000Z"], stdin=stream,
                                  capture_output=True, text=True, check=True)
        rows, last = restore.check(backup)
        self.assertEqual(done.stdout.strip(), f"{rows} rows to sequence {last}")
        self.assertEqual((data / "kernel.sqlite3").read_bytes(), backup.read_bytes())
        self.assertEqual({path.name: path.read_bytes() for path in data.iterdir() if path.name != "kernel.sqlite3"},
                         {"kernel.sqlite3.before-restore-20261116T120000Z": b"the journal before",
                          "kernel.sqlite3-wal.before-restore-20261116T120000Z": b"its log", "devices": b"tokens"})

    def test_a_restore_that_does_not_read_back_as_the_backup_fails_and_says_where_the_old_journal_is(self):
        with self.assertRaisesRegex(SystemExit, "before-restore-20261116T120000Z"):
            restore.restore(Docker(reads_back="1 rows to sequence 1"), self.backup(), "20261116T120000Z")

    def test_the_makefile_has_both_and_a_restore_needs_a_named_backup_from_the_main_checkout(self):
        makefile = (ROOT / "Makefile").read_text()
        self.assertRegex(makefile, r"(?m)^restore-drill:\n\t\$\(PYTHON\) make/restore\.py drill")
        self.assertRegex(makefile, r"(?m)^restore:\n\t\$\(MAIN_ONLY\)\n\t@\[ -n \"\$\(BACKUP\)\" \]")


if __name__ == "__main__":
    unittest.main()
