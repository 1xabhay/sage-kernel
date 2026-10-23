"""The containers stay sealed: loopback-only ports, no root, read-only, bounded, nothing mounted from the host."""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = (("compose.yaml", "kernel"), ("compose.test.yaml", "conformance"))


def service(name: str, file: str = "compose.yaml") -> str:
    """The text of one top-level service block in a compose file (stdlib only, no YAML parser)."""
    text = (ROOT / file).read_text()
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"{file} has no {name} service")
    return match.group(1)


def recipe(target: str) -> str:
    """The commands of one Makefile target."""
    match = re.search(rf"^{target}:.*\n((?:\t.*\n?)*)", (ROOT / "Makefile").read_text(), re.MULTILINE)
    if match is None:
        raise AssertionError(f"Makefile has no {target} target")
    return match.group(1)


@unittest.skipUnless((ROOT / "compose.yaml").exists(), "checks the source tree; the image ships without it")
class DeployTests(unittest.TestCase):
    def test_every_published_port_binds_to_loopback_only(self):
        ports = re.findall(r'^\s+- "([^"]+)"$', service("kernel").split("ports:", 1)[-1].split(":\n", 1)[0],
                           re.MULTILINE)
        self.assertTrue(ports)
        for port in ports:
            with self.subTest(port=port):
                self.assertTrue(port.startswith("127.0.0.1:"), "another machine could reach this port")
                self.assertNotIn(port.split(":")[1], ("8765", "8766"), "those host ports belong to the old kernel")
        self.assertNotIn("ports:", service("conformance", "compose.test.yaml"))

    def test_containers_run_unprivileged_on_a_read_only_filesystem(self):
        for file, name in SERVICES:
            block = service(name, file)
            with self.subTest(service=name):
                self.assertIn("read_only: true", block)
                self.assertIn("no-new-privileges:true", block)
                self.assertRegex(block, r"cap_drop:\n\s+- ALL")
                self.assertRegex(block, r"pids_limit: \d+")
                self.assertRegex(block, r"mem_limit: \d+[mg]")
                self.assertRegex(block, r"tmpfs:\n\s+- /tmp:")
        self.assertRegex((ROOT / "Dockerfile").read_text(), r"(?m)^USER 1000:1000$")
        self.assertRegex((ROOT / "Dockerfile").read_text(), r"(?m)^FROM python:3\.12[\w.-]*@sha256:")

    def test_nothing_is_mounted_from_the_host(self):
        for file, name in SERVICES:
            with self.subTest(service=name):
                block = service(name, file)
                self.assertNotIn("type: bind", block)
                mounts = re.search(r"volumes:\n((?:\s+- .*\n)*)", block)
                for mount in re.findall(r"- (\S+)", mounts.group(1) if mounts else ""):
                    self.assertRegex(mount, r"^[a-z][\w-]*:/", "only named volumes: Colima cannot see this folder")


@unittest.skipUnless((ROOT / "compose.yaml").exists(), "checks the source tree; the image ships without it")
class LiveDataTests(unittest.TestCase):
    """The live kernel's journal (the kernel-data volume) is reachable only through make up and make down."""

    def test_tests_run_in_their_own_compose_project_with_no_volumes(self):
        live, test = (ROOT / "compose.yaml").read_text(), (ROOT / "compose.test.yaml").read_text()
        self.assertRegex(live, r"(?m)^name: sage-kernel$")
        self.assertRegex(test, r"(?m)^name: sage-kernel-test$")
        self.assertNotIn("volumes:", test)
        self.assertNotIn("kernel-data", test)
        self.assertNotRegex(live, r"(?m)^  conformance:")
        self.assertNotIn("image: sage-kernel:", test)  # tests never retag the live image either

    def test_test_targets_use_only_the_test_project(self):
        for target in ("unit", "conformance", "eval"):
            with self.subTest(target=target):
                commands = recipe(target)
                for line in re.findall(r"docker compose[^\n]*", commands):
                    self.assertIn("-f compose.test.yaml", line)

    def test_no_target_can_remove_a_volume(self):
        makefile = (ROOT / "Makefile").read_text()
        self.assertNotRegex(makefile, r"down\s+(-\w*v|--volumes)|volume\s+rm|\bprune\b|--remove-orphans")
        self.assertEqual(recipe("down").strip(), "docker compose down")


PRIVATE = (".env", ".env.local", ".backups/kernel.sqlite3", "evals/local/run.json", "bench/.local/hf/model.bin",
           "bench/expressive/.local/run.sqlite3", "bench/.venv/bin/python", ".claude/worktrees/lane/x", ".deploy/abc/x")


@unittest.skipUnless((ROOT / ".git").exists(), "checks the git checkout")
class PublishTests(unittest.TestCase):
    """Nothing private or cached is ever committed or sent to the image build, and no tracked file names this
    machine: secrets, backups, live speech (evals/local/) and the bench's model caches stay local."""

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], check=False, cwd=ROOT, capture_output=True, text=True)

    def test_private_and_cached_paths_are_ignored_by_git(self):
        for path in PRIVATE:
            with self.subTest(path=path):
                self.assertEqual(self.git("check-ignore", "-q", "--no-index", path).returncode, 0)
        self.assertNotEqual(self.git("check-ignore", "-q", "--no-index", ".env.example").returncode, 0)

    def test_private_and_cached_paths_never_reach_the_image_build(self):
        ignored = set((ROOT / ".dockerignore").read_text().split())
        for pattern in (".env", ".env.*", ".backups", "evals/local", "bench", "**/.venv", "**/.local", ".git"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, ignored)

    def test_no_tracked_file_names_a_lan_address_or_a_home_folder(self):
        # The same scanner the pre-push hook runs over the added lines: one list of patterns.
        for mode in ("--self-test", "--tree"):
            with self.subTest(mode=mode):
                run = subprocess.run([sys.executable, ".githooks/hygiene.py", mode], check=False, cwd=ROOT,
                                     capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)

@unittest.skipUnless((ROOT / ".git").exists(), "checks the git checkout")
class PrePushTests(unittest.TestCase):
    """The pre-push hook keeps local-only branches local and refuses to publish an added private address."""

    def setUp(self):
        self.repo = tempfile.TemporaryDirectory()
        self.addCleanup(self.repo.cleanup)
        self.git("init", "-q")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], check=True,
                              cwd=self.repo.name, capture_output=True, text=True).stdout.strip()

    def commit(self, text: str) -> str:
        (Path(self.repo.name) / "f.txt").write_text(text)
        self.git("add", "f.txt")
        self.git("commit", "-qm", "c")
        return self.git("rev-parse", "HEAD")

    def push(self, local_ref: str, sha: str, remote_ref: str = "") -> int:
        line = f"{local_ref} {sha} {remote_ref or local_ref} {'0' * 40}\n"
        return subprocess.run(["sh", str(ROOT / ".githooks/pre-push"), "origin", "url"], input=line, text=True,
                              cwd=self.repo.name, capture_output=True, check=False).returncode

    def test_local_only_branches_are_refused(self):
        sha = self.commit("hello\n")
        for ref in ("refs/heads/history/sage", "refs/heads/reachy/sage-voice", "refs/heads/backup/x",
                    "refs/heads/private-history"):
            with self.subTest(ref=ref):
                self.assertNotEqual(self.push(ref, sha), 0)
        self.assertNotEqual(self.push("refs/heads/topic", sha, "refs/heads/history/sage"), 0)
        self.assertEqual(self.push("refs/heads/repo/guards", sha), 0)

    def test_an_added_private_address_is_refused(self):
        self.commit("hello\n")
        sha = self.commit("hello\nkernel at " + "192.168" + ".1.20\n")
        self.assertNotEqual(self.push("refs/heads/repo/topic", sha), 0)
        sha = self.commit("hello\n")  # removing it again does not make the range clean: the add is still pushed
        self.assertNotEqual(self.push("refs/heads/repo/topic", sha), 0)

    def test_make_hooks_enables_the_tracked_hooks(self):
        self.assertIn("git config core.hooksPath .githooks", recipe("hooks"))

@unittest.skipUnless((ROOT / ".git").exists(), "checks the git checkout")
class CommitGateTests(unittest.TestCase):
    """The commit-msg gate runs `make test` of the project nearest each staged path; (red) lets a failure in."""

    def setUp(self):
        self.repo = tempfile.TemporaryDirectory()
        self.addCleanup(self.repo.cleanup)
        self.root = Path(self.repo.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / "Makefile").write_text("test:\n\ttrue\n")
        (self.root / "app" / "src").mkdir(parents=True)
        (self.root / "app" / "Makefile").write_text("test:\n\tfalse\n")

    def gate(self, path: str, subject: str) -> int:
        (self.root / path).write_text("x\n")
        subprocess.run(["git", "add", path], cwd=self.root, check=True)
        (self.root / "MSG").write_text(subject + "\n\nbody\n")
        return subprocess.run(["sh", str(ROOT / ".githooks/commit-msg"), "MSG"], cwd=self.root,
                              capture_output=True, check=False).returncode

    def test_the_nearest_project_must_pass(self):
        self.assertEqual(self.gate("top.txt", "Root change"), 0)  # only the root project ran
        self.assertNotEqual(self.gate("app/src/a.txt", "App change"), 0)

    def test_a_red_subject_lets_failing_tests_in(self):
        self.assertEqual(self.gate("app/src/a.txt", "App test first (red)"), 0)
        self.assertNotEqual(self.gate("app/src/a.txt", "App (red) in the middle"), 0)

    def test_make_test_runs_the_kernel_suites(self):
        self.assertEqual(recipe("test").strip(), "")  # the work is in its prerequisites
        self.assertRegex((ROOT / "Makefile").read_text(), r"(?m)^test: unit conformance$")
        self.assertRegex((ROOT / "Makefile").read_text(), r"(?m)^check: test$")


if __name__ == "__main__":
    unittest.main()
