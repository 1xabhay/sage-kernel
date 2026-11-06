"""`make deploy`, `make env` and `make release`, without deploying or uploading anything.

Every make here runs with HOME in a temp folder and the make variables scrubbed, so a default
can only land in that folder; the deploy and release checks run in throwaway git repos.
"""

import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
TEMPLATE = APP / "deploy/launchd.plist.in"
MAKE_VARIABLES = {"SHA", "LABEL", "LOGS", "ENV_FILE", "PLIST", "DRY_RUN", "HF", "SPACE", "EXPORT"}
PLACEHOLDERS = {"@LABEL@", "@APP@", "@SHA@", "@LOG@", "@PATH@"}
MAIN = "reachy_sage_voice.__main__"
PREFIX = "devices/reachy-mini/apps/reachy_sage_voice"
SHARED = "devices/common/sage_device"  # the app's uv path dependency
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]


def make(cwd: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MAKE", "MFLAGS", "SAGE_", "DEPLOY_"))
           and k not in MAKE_VARIABLES}
    env.update(HOME=str(home), HF="false")  # false: an upload that slips past DRY_RUN fails
    return subprocess.run(["make", "-s", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=60)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def repo(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    """Make a throwaway repo with the app's Makefile, deploy/ and scanner, laid out as here."""
    app = tmp_path / "repo" / PREFIX
    for name in ("Makefile", "pyproject.toml", ".env.example", "README.md", "audit-ignore.txt",
                 "tests/test_hygiene.py"):
        (app / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(APP / name, app / name)
    shutil.copytree(APP / "deploy", app / "deploy")
    shared = APP.parents[3] / SHARED
    for name in git(shared, "ls-files").split():
        (tmp_path / "repo" / SHARED / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(shared / name, tmp_path / "repo" / SHARED / name)
    for name, text in (extra or {}).items():
        (app / name).parent.mkdir(parents=True, exist_ok=True)
        (app / name).write_text(text)
    git(tmp_path / "repo", "init", "-q")
    git(tmp_path / "repo", "add", "-f", ".")
    git(tmp_path / "repo", "commit", "-qm", "init")
    return app


def version() -> str:
    import tomllib

    return tomllib.loads((APP / "pyproject.toml").read_text())["project"]["version"]


def path_sources() -> list[str]:
    import tomllib

    sources = tomllib.loads((APP / "pyproject.toml").read_text())["tool"]["uv"]["sources"]
    return [s["path"] for s in sources.values() if "path" in s]


# The launchd template


def test_the_template_keeps_the_voice_alive_and_names_no_home():
    text = TEMPLATE.read_text()
    plist = plistlib.loads(text.encode())
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["ThrottleInterval"] == 10
    assert set(re.findall(r"@[A-Z]+@", text)) == PLACEHOLDERS
    assert "/Users/" not in text and "/home/" not in text and "~" not in text


def test_the_template_runs_the_exports_own_venv_with_no_settings():
    plist = plistlib.loads(TEMPLATE.read_bytes())
    shell, flag, command = plist["ProgramArguments"]
    assert (shell, flag) == ("/bin/sh", "-c")
    # The Mac stays awake while the voice runs, and the voice stays the pid launchd watches.
    assert command == ('/usr/bin/caffeinate -i -s -w $$ & '
                       'exec "@APP@/.venv/bin/python" -u -m reachy_sage_voice')
    assert plist["WorkingDirectory"] == "@APP@"
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == "@LOG@"
    # Settings only from the .env: the plist carries PATH and the build (pet.py), so never a token.
    assert plist["EnvironmentVariables"] == {"PATH": "@PATH@", "SAGE_VOICE_BUILD": "@SHA@"}


def test_rendering_substitutes_every_placeholder(tmp_path):
    out = tmp_path / "out.plist"
    done = make(APP, tmp_path, "plist", "EXPORT=/x/voice/abc1234/app", "SHORT=abc1234",
                f"PLIST={out}")
    assert done.returncode == 0, done.stderr
    text = out.read_text()
    assert re.findall(r"@[A-Z]+@", text) == []
    plist = plistlib.loads(text.encode())
    assert plist["Label"] == "ai.sage.reachy-voice"
    assert plist["WorkingDirectory"] == "/x/voice/abc1234/app"
    assert plist["StandardOutPath"] == f"{tmp_path}/Library/Logs/reachy-sage/bot-abc1234.log"
    if shutil.which("plutil"):
        assert subprocess.run(["plutil", "-lint", "-s", str(out)]).returncode == 0


# make env


@pytest.mark.skipif(sys.platform != "darwin", reason="ENV_FILE's default is the macOS one")
def test_env_writes_the_apps_own_dotenv_once_at_600(tmp_path):
    # The app's own answer, in a process of its own: conftest stubs env_files() here.
    target = Path(subprocess.run(
        [sys.executable, "-c", f"from {MAIN} import env_files; print(env_files()[0])"],
        cwd=APP, env={**os.environ, "HOME": str(tmp_path)}, check=True, capture_output=True,
        text=True).stdout.strip())
    assert str(target).startswith(str(tmp_path))
    assert make(APP, tmp_path, "env").returncode == 0
    assert target.read_text() == (APP / ".env.example").read_text()
    assert target.stat().st_mode & 0o777 == 0o600
    target.write_text("SAGE_VOICE_KERNEL_TOKEN=kept\n")
    assert make(APP, tmp_path, "env").returncode == 0
    assert target.read_text() == "SAGE_VOICE_KERNEL_TOKEN=kept\n"


# make deploy: the refusals, all before anything is exported or loaded


def test_deploy_refuses_a_dirty_tree(tmp_path):
    app = repo(tmp_path)
    (app / "README.md").write_text("changed\n")
    done = make(app, tmp_path, "deploy")
    assert done.returncode != 0
    assert "dirty" in done.stderr
    assert not (tmp_path / "repo/.deploy").exists()
    assert not (tmp_path / "Library/LaunchAgents").exists()


def test_deploy_refuses_a_sha_that_is_not_committed(tmp_path):
    app = repo(tmp_path)
    done = make(app, tmp_path, "deploy", "SHA=deadbeef")
    assert done.returncode != 0
    assert "not a commit" in done.stderr
    assert not (tmp_path / "repo/.deploy").exists()


def test_deploy_refuses_without_the_dotenv(tmp_path):
    app = repo(tmp_path, {"../../../../.gitignore": ".deploy/\n"})
    done = make(app, tmp_path, "deploy")
    assert done.returncode != 0
    assert "make env" in done.stderr
    assert not (tmp_path / "repo/.deploy").exists()


def test_deploy_refuses_when_deploy_is_not_ignored(tmp_path):
    app = repo(tmp_path)
    done = make(app, tmp_path, "deploy")
    assert done.returncode != 0
    assert ".deploy/" in done.stderr


def test_the_export_carries_the_path_dependencies_the_lock_needs(tmp_path):
    # Live 2026-10-25: the export had only the app, so uv sync --locked found no sage_device.
    app = repo(tmp_path)
    out = tmp_path / "export"
    done = subprocess.run(["sh", "deploy/deploy.sh", "export", "HEAD", str(out)], cwd=app,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert (out / PREFIX / "pyproject.toml").is_file()
    assert path_sources()
    for path in path_sources():  # at the same relative path, so the lock's path still holds
        assert (out / PREFIX / path / "pyproject.toml").is_file(), path
        assert (out / PREFIX / path / "sage_device/__init__.py").is_file(), path


# make release


def released(done: subprocess.CompletedProcess) -> set[str]:
    lines = done.stdout.splitlines()
    return {line.removeprefix("release: ") for line in lines if line.startswith("release: ")}


def test_release_refuses_a_version_mismatch(tmp_path):
    app = repo(tmp_path)
    assert make(app, tmp_path, "release", "DRY_RUN=1").returncode != 0  # no tag at all
    git(app, "tag", "voice/v9.9.9")
    done = make(app, tmp_path, "release", "DRY_RUN=1")
    assert done.returncode != 0
    assert f"voice/v{version()}" in done.stderr
    assert released(done) == set()


def test_release_ships_the_whitelist_only(tmp_path):
    app = repo(tmp_path, {
        ".env": "SAGE_VOICE_KERNEL_TOKEN=x\n",  # force-added: tracked or not, it never ships
        "index.html": "<p>hi</p>\n", "uv.lock": "", "LICENSE": "", "NOTICE": "",
        "reachy_sage_voice/__init__.py": "", "services/compose.yaml": "",
        "services/profiles/owner.npy": "", "services/profiles/recordings/turn-1.wav": "",
        "services/stt/test_app.py": "", "services/stt/fixtures/a.wav": "",
        "tests/test_x.py": "", "eval/recordings/a.wav": "", "recordings/b.wav": "",
        "bench/x.py": "", "docs/PLAN.md": "",
    })
    git(app, "tag", f"voice/v{version()}")
    done = make(app, tmp_path, "release", "DRY_RUN=1")
    assert done.returncode == 0, done.stderr
    files = released(done)
    assert {"README.md", "index.html", "LICENSE", "NOTICE", "pyproject.toml", "uv.lock",
            ".env.example", "Makefile", "reachy_sage_voice/__init__.py",
            "services/compose.yaml"} <= files
    assert not [f for f in files if f == ".env" or f.endswith(".wav") or "profiles" in f
                or f.startswith(("tests/", "eval/", "bench/", "docs/", "deploy/")) or "test_" in f]
    assert "DRY_RUN" in done.stdout


def test_release_vendors_the_path_dependencies(tmp_path):
    # The Space has no devices/common: the shared package ships inside, its path rewritten.
    app = repo(tmp_path, {"uv.lock": (APP / "uv.lock").read_text()})
    git(app, "tag", f"voice/v{version()}")
    done = make(app, tmp_path, "release", "DRY_RUN=1")
    assert done.returncode == 0, done.stderr
    files = released(done)
    assert {"vendor/sage_device/pyproject.toml", "vendor/sage_device/sage_device/__init__.py",
            "vendor/sage_device/sage_device/names.py"} <= files
    assert not [f for f in files if "test_" in f]
    for path in path_sources():
        assert f"vendored {path} as vendor/sage_device" in done.stdout


def test_release_refuses_an_export_that_names_a_home_folder(tmp_path):
    app = repo(tmp_path, {"reachy_sage_voice/x.py": "PATH = '/Users" + "/someone/x'\n"})
    git(app, "tag", f"voice/v{version()}")
    done = make(app, tmp_path, "release", "DRY_RUN=1")
    assert done.returncode != 0
    assert "reachy_sage_voice/x.py" in done.stderr


def test_the_export_is_tested_as_no_git_checkout():
    # Live 2026-10-23: the export sits in the main checkout's .deploy/, so git found that repo:
    # test_tracked saw an untracked dir and kernel_dir() picked the main checkout's kernel.
    script = (APP / "deploy" / "deploy.sh").read_text()
    test_line = next(line for line in script.splitlines() if "pytest" in line)
    assert "GIT_CEILING_DIRECTORIES" in test_line
