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
        self.assertNotIn("build:", test)  # tests never build or retag an image: they run the one make built

    def test_test_targets_use_only_the_test_project(self):
        for target in ("unit", "conformance", "eval"):
            with self.subTest(target=target):
                commands = recipe(target)
                for line in re.findall(r"docker compose[^\n]*", commands):
                    self.assertIn("-f compose.test.yaml", line)

    def test_no_target_can_remove_a_volume(self):
        makefile = (ROOT / "Makefile").read_text()
        self.assertNotRegex(makefile, r"down\s+(-\w*v|--volumes)|volume\s+rm|\bprune\b|--remove-orphans")
        self.assertRegex(recipe("down").strip().splitlines()[-1].strip(), r"^(SAGE_TAG=\S+ )?docker compose down$")

    def test_the_journal_volume_keeps_its_name_wherever_compose_yaml_sits(self):
        # The volume that exists today: pinned, so moving compose.yaml (or the project) never makes a new one.
        self.assertRegex((ROOT / "compose.yaml").read_text(),
                         r"(?m)^volumes:\n  kernel-data:\n    name: sage-kernel_kernel-data$")

    def test_each_lane_tests_in_its_own_project(self):
        makefile = (ROOT / "Makefile").read_text()
        self.assertRegex(makefile, r"(?m)^TEST := .*docker compose -p sage-kernel-test-\$\(LANE\) -f compose.test.yaml$")

    def test_up_and_down_run_only_from_the_main_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            main, lane, bin_ = Path(tmp, "main"), Path(tmp, "lane"), Path(tmp, "bin")
            main.mkdir()
            bin_.mkdir()
            (bin_ / "docker").write_text('#!/bin/sh\necho "$*" >> "$DOCKER_CALLS"\n')  # a fake: never the real one
            (bin_ / "docker").chmod(0o755)
            (main / "Makefile").write_text((ROOT / "Makefile").read_text())
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run([*git, "init", "-q"], cwd=main, check=True)
            subprocess.run([*git, "add", "Makefile"], cwd=main, check=True)
            subprocess.run([*git, "commit", "-qm", "m"], cwd=main, check=True)
            subprocess.run([*git, "worktree", "add", "-q", str(lane)], cwd=main, check=True)
            calls = Path(tmp, "calls")
            env = {"PATH": f"{bin_}:/usr/bin:/bin", "DOCKER_CALLS": str(calls), "HOME": tmp}
            for target in ("up", "deploy", "up-tag", "down"):
                with self.subTest(target=target):
                    run = subprocess.run(["make", "-s", target], cwd=lane, env={**env, "SAGE_TAG": "abc"},
                                         capture_output=True, check=False)
                    self.assertNotEqual(run.returncode, 0)
                    self.assertFalse(calls.exists(), "a linked worktree reached docker")
            run = subprocess.run(["make", "-s", "down"], cwd=main, env=env, capture_output=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(calls.exists())


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
        # An allow-list: everything is out, then only what the image copies comes back, minus caches and secrets.
        lines = [line for line in (ROOT / ".dockerignore").read_text().splitlines() if line and line[0] != "#"]
        self.assertEqual(lines[0], "*")
        self.assertEqual({line for line in lines if line[0] == "!"}, {"!sage", "!tests", "!conformance", "!evals"})
        last_include = max(i for i, line in enumerate(lines) if line[0] == "!")
        for pattern in ("**/.env", "**/.env.*", "**/.local", "**/.venv", "**/__pycache__", "evals/local"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, lines[last_include:])

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

# ponytail: these are red until repo/experiments merges (it names them, drops `down -v`, and moves
# music-telling to experiments/); delete this list then.
NOT_YET = {"apps/music-telling/docker-compose.yml", "apps/music-telling/Makefile", "experiments/sage-os/compose.yaml"}


@unittest.skipUnless((ROOT / ".git").exists(), "checks the git checkout")
class RepoWideComposeTests(unittest.TestCase):
    """Every project in the repo pins its compose project name and no Makefile can delete a volume."""

    def tracked(self, pattern: str) -> list[str]:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True).stdout
        return [p for p in out.split("\0") if re.search(pattern, p) and p not in NOT_YET]

    def test_every_compose_file_pins_its_project_name(self):
        files = self.tracked(r"(^|/)(docker-)?compose[\w.-]*\.ya?ml$")
        self.assertIn("compose.yaml", files)
        for path in files:
            with self.subTest(path=path):
                self.assertRegex((ROOT / path).read_text(), r"(?m)^name: \S+")

    def test_no_makefile_can_remove_a_volume(self):
        files = self.tracked(r"(^|/)Makefile$")
        self.assertIn("Makefile", files)
        for path in files:
            with self.subTest(path=path):  # find's -prune is fine; docker's prune is not
                self.assertNotRegex((ROOT / path).read_text(), r"down\s+(-\w*v|--volumes)|volume rm|(?<![-\w])prune")

@unittest.skipUnless((ROOT / "Makefile").exists(), "checks the source tree; the image ships without it")
class SecretScanTests(unittest.TestCase):
    def test_the_secret_scan_reads_a_bundle_so_it_works_in_a_worktree(self):
        # In a linked worktree .git is a file pointing elsewhere: tarring it sends gitleaks nothing to read.
        commands = recipe("secrets")
        self.assertIn("git bundle create --quiet - --all", commands)
        self.assertNotIn("tar -c .git", commands)

@unittest.skipUnless((ROOT / "Makefile").exists(), "checks the source tree; the image ships without it")
class HygieneTests(unittest.TestCase):
    """make hygiene is exactly what CI runs, including these host checks that skip inside the image."""

    def test_hygiene_runs_the_host_checks_and_ci_runs_only_hygiene(self):
        makefile = (ROOT / "Makefile").read_text()
        self.assertRegex(makefile, r"(?m)^hygiene: source docs lint vuln secrets test$")
        self.assertIn("-m unittest tests.test_deploy tests.test_boundary", recipe("source"))
        runs = re.findall(r"(?m)^\s+run: (.*)$", (ROOT / ".github/workflows/ci.yml").read_text())
        self.assertEqual(runs, ["make hygiene"])


def run(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False, **kw)


@unittest.skipUnless((ROOT / "Makefile").exists(), "checks the source tree; the image ships without it")
class ReleaseTests(unittest.TestCase):
    """One image per commit: built from git archive of a clean HEAD, tested, then deployed unchanged."""

    def test_live_and_test_run_the_same_image_by_tag_and_never_build_or_pull(self):
        for file, name in SERVICES:
            with self.subTest(file=file):
                block = service(name, file)
                self.assertRegex(block, r"(?m)^    image: sage-kernel:\$\{SAGE_TAG:\?[^}]+\}$")
                self.assertIn("pull_policy: never", block)
                self.assertNotIn("build:", (ROOT / file).read_text())

    def test_the_image_comes_from_a_clean_commit(self):
        commands = recipe("image")
        self.assertIn("git status --porcelain --untracked-files=no", commands)
        self.assertIn("git archive HEAD | docker build -t sage-kernel:$(SHA) -", commands)
        for line in (ROOT / "Dockerfile").read_text().splitlines():
            if line.startswith("COPY"):
                self.assertIn("--chmod=", line)  # the host umask cannot make the code unreadable to uid 1000

    def test_nothing_builds_in_the_test_or_deploy_path(self):
        for target in ("unit", "conformance", "eval", "deploy", "up-tag"):
            with self.subTest(target=target):
                self.assertNotIn("--build", recipe(target))
        self.assertRegex((ROOT / "Makefile").read_text(), r"(?m)^up: deploy$")

    def test_deploy_is_image_test_backup_then_up_checked_and_logged(self):
        commands = recipe("deploy")
        self.assertTrue(commands.lstrip().startswith("$(MAIN_ONLY)"))
        self.assertIn("$(MAKE) image test backup", commands)
        self.assertIn("SAGE_TAG=$(SHA) docker compose up -d --wait kernel", commands)
        self.assertIn("$(DEPLOY_LOG)", commands)
        self.assertIn("$(CHECK_LIVE)", commands)
        self.assertIn("sage-kernel_kernel-data", (ROOT / "Makefile").read_text())
        self.assertRegex((ROOT / "Makefile").read_text(), r"(?m)^DEPLOY_LOG \?= \$\(HOME\)/")
        self.assertIn("$(CHECK_LIVE)", recipe("up-tag"))

    def test_backup_reads_the_live_journal_read_only(self):
        commands = recipe("backup")
        self.assertIn("mode=ro", commands)
        self.assertIn(".serialize()", commands)
        self.assertIn("integrity_check", commands)
        self.assertIn("umask 077", commands)
        self.assertNotRegex(commands, r"docker (compose )?(cp|stop|restart|rm)\b")

    def test_release_check_and_conventions_pass_their_self_tests(self):
        for script in ("make/release_check.py", "make/conventions.py"):
            with self.subTest(script=script):
                result = run(sys.executable, script, "--self-test")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_kernel_meets_the_project_conventions(self):
        result = run(sys.executable, "make/conventions.py", "--strict", ".")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_root_fans_out_to_every_project(self):
        makefile = (ROOT / "Makefile").read_text()
        self.assertRegex(makefile, r"(?m)^PROJECTS := \. devices/reachy-mini experiments/sage-os "
                                   r"\$\(wildcard apps/music-telling experiments/music-telling\)$")
        self.assertIn("for p in $(PROJECTS); do", makefile)
        for target in ("test-all", "hygiene-all"):
            with self.subTest(target=target):
                self.assertIn(f"$(call FAN_OUT,{target.split('-')[0]})", recipe(target))
        self.assertIn("make/conventions.py", recipe("conventions"))

    def test_smoke_walks_the_readme_against_a_throwaway_kernel(self):
        result = run("make", "-s", "smoke", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("smoke: ok", result.stdout)
        self.assertNotIn(":8776", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
