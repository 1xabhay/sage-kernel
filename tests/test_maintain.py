"""make prune and the nightly ai.sage.maintain agent: what goes, what always stays.

Host-only (make source): the image ships without make/. Nothing here touches real Docker or the
checkout's .deploy and .backups: a fake docker on PATH and a temp folder stand in for them.
"""

import datetime as dt
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRUNE = ROOT / "make" / "prune.py"
TEMPLATE = ROOT / "make" / "maintain.plist.in"

SOURCE = (ROOT / "Makefile").exists()  # the checkout, not the image
if SOURCE:
    sys.path.insert(0, str(PRUNE.parent))
    import prune

# The fake docker: canned image and container lists, and each rmi logged.
FAKE_DOCKER = """#!/bin/sh
case "$1" in
images) printf '%s\\n' \
  'sage-kernel\tabc1\t2026-11-01 10:00:00 -0500 EST' 'sage-kernel\tabc2\t2026-11-02 10:00:00 -0500 EST' \
  'sage-kernel\tabc3\t2026-11-03 10:00:00 -0500 EST' 'sage-kernel\tabc4\t2026-11-04 10:00:00 -0500 EST' \
  'sage-kernel\tdev-a\t2026-11-09 10:00:00 -0500 EST' 'sage-kernel\tdev-b\t2026-11-09 11:00:00 -0500 EST' \
  'sage-kernel\t0.1.0\t2026-10-01 10:00:00 -0500 EST' 'sage-kernel\t<none>\t2026-09-01 10:00:00 -0500 EST' \
  'reachy-sage-stt\told\t2026-09-01 10:00:00 -0500 EST' 'postgres\t17\t2026-01-01 10:00:00 -0500 EST' \
  'sage-kernel-test-x-conformance\tlatest\t2026-01-01 10:00:00 -0500 EST' ;;
ps) echo sage-kernel:0.1.0 ;;
*) echo "$@" >> "$DOCKER_LOG" ;;
esac
"""


@unittest.skipUnless(SOURCE, "checks the source tree; the image ships without make/")
class StaleTests(unittest.TestCase):
    def test_all_but_the_newest_go_and_one_in_use_stays(self):
        dates = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}
        self.assertEqual(prune.stale(dates, 3), ["a", "b"])
        self.assertEqual(prune.stale(dates, 3, using={"a"}), ["b"])
        self.assertEqual(prune.stale(dates, 9), [])
        self.assertEqual(prune.stale({}, 3), [])

    def test_a_session_goes_only_once_its_name_is_more_than_a_week_old(self):
        today = dt.date(2026, 11, 9)
        names = ["session-20261101-235959", "session-20261102-000000", "session-20261109-080000",
                 "notes.txt", "session-2026"]
        self.assertEqual(prune.old_sessions(names, today, 7), ["session-20261101-235959"])


@unittest.skipUnless(SOURCE, "checks the source tree; the image ships without make/")
class PruneRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.root = self.tmp / "root"
        voice = self.root / ".deploy" / "voice"
        for i, sha in enumerate(["aaaaaaa", "bbbbbbb", "ccccccc", "ddddddd"]):
            (voice / sha).mkdir(parents=True)
            os.utime(voice / sha, (1_000_000 + i, 1_000_000 + i))
        (voice / "session-20200101-000000").mkdir()  # not an export: never touched here
        recordings = self.root / ".deploy" / "recordings"
        for name in ("session-20200101-000000", "session-20991231-000000", "keep.txt"):
            (recordings / name).mkdir(parents=True)
        backups = self.root / ".backups"
        backups.mkdir()
        for day in range(1, 17):
            (backups / f"journal-202611{day:02d}T000000Z.sqlite3").write_text("x")
        (backups / "pre-int2-20261108-125917").write_text("the owner's own")
        self.plist = self.tmp / "voice.plist"  # the voice runs the oldest export
        self.plist.write_bytes(plistlib.dumps({"WorkingDirectory": str(voice / "aaaaaaa" / "app")}))

    def run_prune(self, **env):
        return subprocess.run(
            [sys.executable, str(PRUNE), str(self.root)], check=False, capture_output=True, text=True, timeout=30,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "VOICE_PLIST": str(self.plist),
                 "DOCKER_LOG": str(self.tmp / "docker.log"), **env})

    def test_it_keeps_the_newest_and_the_running_and_deletes_the_rest(self):
        done = self.run_prune()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.tmp / "docker.log").read_text().splitlines(),
                         ["rmi sage-kernel:abc1"])  # 0.1.0 runs; dev-* builds count apart; never <none> or others
        self.assertEqual(sorted(p.name for p in (self.root / ".deploy" / "voice").iterdir()),
                         ["aaaaaaa", "ccccccc", "ddddddd", "session-20200101-000000"])
        self.assertEqual(sorted(p.name for p in (self.root / ".deploy" / "recordings").iterdir()),
                         ["keep.txt", "session-20991231-000000"])
        backups = sorted(p.name for p in (self.root / ".backups").iterdir())
        self.assertEqual(len(backups), 15)
        self.assertIn("pre-int2-20261108-125917", backups)
        self.assertNotIn("journal-20261101T000000Z.sqlite3", backups)

    def test_dry_run_lists_and_deletes_nothing(self):
        done = self.run_prune(DRY_RUN="1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("would delete sage-kernel:abc1", done.stdout)
        self.assertFalse((self.tmp / "docker.log").exists())
        self.assertEqual(len(list((self.root / ".backups").iterdir())), 17)

    def test_it_never_reaches_for_volumes_or_a_system_wide_prune(self):
        source = PRUNE.read_text()
        self.assertIsNone(re.search(r'"(volume|system|prune|-f|--force)"', source))


@unittest.skipUnless(SOURCE, "checks the source tree; the image ships without make/")
class MaintainAgentTests(unittest.TestCase):
    def test_the_template_backs_up_then_prunes_nightly_and_names_no_home(self):
        text = TEMPLATE.read_text()
        self.assertNotRegex(text, r"/Users/|/home/")
        self.assertIn("make -k backup prune", text)
        self.assertIn("StartCalendarInterval", text)
        self.assertNotIn("KeepAlive", text)

    def test_maintain_plist_renders_every_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "maintain.plist"
            done = subprocess.run(["make", "-s", "maintain-plist", f"MAINTAIN_PLIST={out}"], check=False,
                                  cwd=ROOT, capture_output=True, text=True, timeout=30, env={**os.environ, "HOME": tmp})
            self.assertEqual(done.returncode, 0, done.stderr)
            plist = plistlib.loads(out.read_bytes())
        self.assertEqual(plist["Label"], "ai.sage.maintain")
        self.assertTrue(Path(plist["WorkingDirectory"], "Makefile").exists())
        self.assertNotIn("@", out.name + str(plist))

    def test_maintain_on_and_off_load_and_unload_only_the_maintain_agent(self):
        make = (ROOT / "Makefile").read_text()
        for target in ("maintain-on", "maintain-off"):
            body = re.search(rf"^{target}:.*\n((?:\t.*\n?)*)", make, re.MULTILINE).group(1)
            self.assertIn("$(MAINTAIN_LABEL)" if target == "maintain-off" else "$(MAINTAIN_PLIST)", body)
            self.assertNotRegex(body, r"reachy-voice|reachy-pocket")


if __name__ == "__main__":
    unittest.main()
