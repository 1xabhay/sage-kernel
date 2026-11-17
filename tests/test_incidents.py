"""Things that went wrong once, for the kernel and the repo's tooling, that no other test would catch again.

Each test names the date and what was seen. One still open is a strict expected failure: when it
is fixed its test turns red until the mark is taken off. The voice's are in
devices/reachy-mini/apps/reachy_sage_voice/tests/test_incidents.py."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / ".githooks" / "pre-push").exists() and (ROOT / "bench" / "quiet.py").exists(),
                     "checks the source tree; the image ships without it")
class Open(unittest.TestCase):
    def test_a_commit_message_that_names_a_private_address_is_not_pushed(self):
        """2026-11-10: a private spoken line was quoted in a commit message (b8c8ede, local only). The push guard
        scans what a commit adds and never what its message says, so nothing would have stopped it leaving."""
        with tempfile.TemporaryDirectory() as repo:
            def git(*args):
                return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=repo,
                                      check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-q")
            Path(repo, "f.txt").write_text("hello\n")
            git("add", "f.txt")
            git("commit", "-qm", "Kernel: answer from " + "192.168" + ".1.20 as it was heard")
            line = f"refs/heads/topic {git('rev-parse', 'HEAD')} refs/heads/topic {'0' * 40}\n"
            pushed = subprocess.run(["sh", str(ROOT / ".githooks" / "pre-push"), "origin", "url"], input=line, cwd=repo,
                                    capture_output=True, text=True, check=False)
        self.assertNotEqual(pushed.returncode, 0)

    def test_backup_asks_the_live_docker_context_by_name_whatever_the_default_is(self):
        """2026-10-20: the Docker CLI fell back to the desktop-linux context, the kernel's container was not
        there, and the journal looked deleted. `make backup` names the colima context in every Docker
        command, as sage-up, sage-down and restore do, so the default context is never the one asked."""
        made = not (ROOT / ".backups").exists()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "docker.log")
            fake = Path(tmp, "bin", "docker")
            fake.parent.mkdir()
            fake.write_text('#!/bin/sh\necho "$*" >> "$FAKE_LOG"\n'
                            '[ "$1 $2" = "context show" ] && { echo desktop-linux; exit 0; }\nexit 1\n')
            fake.chmod(0o755)
            env = {"PATH": f"{fake.parent}:/usr/bin:/bin", "HOME": tmp, "FAKE_LOG": str(log)}  # never the real docker
            done = subprocess.run(["make", "backup"], cwd=ROOT, env=env, capture_output=True, text=True, check=False)
            asked = log.read_text().splitlines() if log.exists() else []
        if made and (ROOT / ".backups").exists() and not os.listdir(ROOT / ".backups"):
            (ROOT / ".backups").rmdir()
        self.assertNotEqual(done.returncode, 0)
        self.assertTrue(asked, done.stderr)
        self.assertEqual([call for call in asked if not call.startswith("--context colima ")], [], done.stderr)

    @unittest.expectedFailure  # bench/quiet.py counts spoke_to_noise; failures() has no threshold for it
    def test_the_quiet_bench_fails_when_sage_speaks_to_a_quarter_of_the_lines_not_meant_for_it(self):
        """2026-11-05 and 11-09: Sage spoke to the TV and to talk not meant for it. Measured again on 11-10, the
        live model still put words to 12 of 44 such lines, and the bench that counted them passed."""
        sys.path.insert(0, str(ROOT / "bench"))
        import quiet

        noise = [{"line": "The ferry leaves at nine, so they say.", "reply": "Nine it is." if n < 12 else "",
                  "tools": [] if n < 12 else ["emote"], "first_ms": 5 if n < 12 else None, "to_sage": False}
                 for n in range(44)]
        got = quiet.score(noise)
        self.assertEqual((got["spoke_to_noise"], got["noise"]), (12, 44))
        self.assertTrue(any("noise" in why or "not said to" in why for why in quiet.failures(got, got)))


if __name__ == "__main__":
    unittest.main()
