"""make sage-up and make sage-down: the whole of Sage on or off, and the choice outlives a reboot.

Host-only (make source): the image ships without make/. Nothing here touches Docker, launchd or
Ollama: every command goes to a fake runner that records it and answers from a table.
"""

import plistlib
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "Makefile").exists()  # the checkout, not the image
if SOURCE:
    sys.path.insert(0, str(ROOT / "make"))
    import updown


class Machine:
    """A Mac as the commands see it: what answers, and everything that was run, in order."""

    def __init__(self, vm=True, running=(), missing=(), loaded=()):
        self.vm, self.running, self.missing, self.loaded, self.ran = vm, set(running), set(missing), set(loaded), []

    def __call__(self, command, check=True):
        line = " ".join(command)
        self.ran.append(line)
        code, out = 0, ""
        if line == "colima status":
            code = 0 if self.vm else 1
        elif line == "colima start":
            self.vm = True
        elif "docker" in command and "start" in command:
            code = 1 if command[-1] in self.missing else 0
            self.running |= {command[-1]} - self.missing
        elif "docker" in command and "stop" in command:
            self.running.discard(command[-1])
        elif "inspect" in command:
            out = ("healthy" if "Health" in line else "true") if command[-1] in self.running else "false"
            code = 1 if command[-1] in self.missing else 0
        elif line.startswith("launchctl print "):
            code = 0 if command[-1].rsplit("/", 1)[-1] in self.loaded else 1
        elif line.startswith("launchctl bootstrap "):
            self.loaded.add(Path(command[-1]).stem)
        elif line.startswith("launchctl bootout "):
            self.loaded.discard(command[-1].rsplit("/", 1)[-1])
        if check and code:
            raise SystemExit(f"{line}: failed")
        return SimpleNamespace(returncode=code, stdout=out)

    def index(self, piece):
        return next(i for i, line in enumerate(self.ran) if piece in line)


@unittest.skipUnless(SOURCE, "checks the source tree; the image ships without make/")
class UpDown(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.agents = Path(folder.name)
        for job in updown.JOBS:
            (self.agents / f"{job}.plist").write_text("")
        self.everything = {updown.KERNEL, *updown.SERVICES}

    def up(self, machine):
        updown.up(machine, self.agents, "501", sleep=lambda s: None)

    def down(self, machine, model="qwen3:4b-instruct"):
        updown.down(machine, "501", model)

    def test_down_backs_up_the_journal_then_turns_every_job_off_for_good_then_stops_and_removes_nothing(self):
        machine = Machine(running=self.everything, loaded=updown.JOBS)
        self.down(machine)
        self.assertLess(machine.index("make backup"), machine.index("launchctl disable"))
        for job in (updown.LOGIN, *updown.JOBS):  # disabled first: a reboot must not bring it back
            self.assertLess(machine.index(f"launchctl disable gui/501/{job}"), machine.index("docker --context colima stop"))
        self.assertLess(machine.index("launchctl bootout gui/501/ai.sage.reachy-voice"),
                        machine.index(f"stop {updown.KERNEL}"))  # the voice goes before what it talks to
        self.assertEqual((machine.running, machine.loaded), (set(), set()))
        self.assertIn("ollama stop qwen3:4b-instruct", machine.ran)
        self.assertFalse([line for line in machine.ran if re.search(r"\b(rm|down|prune|kill)\b| -v\b", line)])

    def test_down_with_the_kernel_already_stopped_skips_the_backup_and_still_turns_the_rest_off(self):
        machine = Machine(running={updown.SERVICES[0]}, loaded=updown.JOBS)
        self.down(machine, model="")
        self.assertFalse([line for line in machine.ran if "backup" in line or "ollama" in line])
        self.assertEqual((machine.running, machine.loaded), (set(), set()))

    def test_up_starts_the_vm_and_the_containers_and_loads_the_jobs_only_once_they_are_healthy(self):
        machine = Machine(vm=False)
        self.up(machine)
        self.assertLess(machine.index("colima start"), machine.index(f"start {updown.KERNEL}"))
        healthy = max(i for i, line in enumerate(machine.ran) if "inspect" in line)
        self.assertLess(healthy, machine.index("launchctl bootstrap"))  # no voice crash-looping on a kernel not up
        self.assertLess(machine.index("bootstrap gui/501 " + str(self.agents / "ai.sage.reachy-pocket.plist")),
                        machine.index("bootstrap gui/501 " + str(self.agents / "ai.sage.reachy-voice.plist")))
        for job in (updown.LOGIN, *updown.JOBS):  # a reboot brings it back
            self.assertIn(f"launchctl enable gui/501/{job}", machine.ran)
        self.assertEqual((machine.running, machine.loaded), (self.everything, set(updown.JOBS)))

    def test_up_on_a_sage_that_is_already_up_loads_no_job_twice(self):
        machine = Machine(running=self.everything, loaded=updown.JOBS)
        self.up(machine)
        self.assertFalse([line for line in machine.ran if "bootstrap" in line or line == "colima start"])

    def test_up_with_a_container_that_was_never_made_says_to_deploy_and_loads_no_job(self):
        machine = Machine(missing={updown.SERVICES[1]})
        with self.assertRaisesRegex(SystemExit, "deploy"):
            self.up(machine)
        self.assertFalse([line for line in machine.ran if "launchctl" in line])

    def test_up_with_a_job_that_was_never_installed_says_which_and_how(self):
        (self.agents / "ai.sage.reachy-voice.plist").unlink()
        with self.assertRaisesRegex(SystemExit, "ai.sage.reachy-voice.*deploy"):
            self.up(Machine())

    def test_a_container_that_never_gets_healthy_stops_up_with_its_name(self):
        machine = Machine()
        machine.missing = set()
        real = machine.__call__

        def sick(command, check=True):
            answer = real(command, check)
            return SimpleNamespace(returncode=0, stdout="starting") if "Health" in " ".join(command) else answer
        with self.assertRaisesRegex(SystemExit, updown.KERNEL):
            updown.up(sick, self.agents, "501", sleep=lambda s: None)

    def test_the_login_job_runs_up_once_at_login_and_is_never_kept_alive(self):
        text = (ROOT / "make" / "up.plist.in").read_text()
        for key, value in {"@LABEL@": updown.LOGIN, "@ROOT@": "/sage", "@PATH@": "/bin", "@LOG@": "/tmp/up.log"}.items():
            text = text.replace(key, value)
        job = plistlib.loads(text.encode())
        self.assertEqual((job["Label"], job["RunAtLoad"], job["WorkingDirectory"]), ("ai.sage.up", True, "/sage"))
        self.assertIn("make sage-up", job["ProgramArguments"][-1])
        self.assertNotIn("KeepAlive", job)

    def test_the_makefile_has_both_and_they_run_only_from_the_main_checkout(self):
        makefile = (ROOT / "Makefile").read_text()
        for target in ("sage-up", "sage-down"):
            self.assertRegex(makefile, rf"(?m)^{target}:.*\n\t\$\(MAIN_ONLY\)\n")


if __name__ == "__main__":
    unittest.main()
