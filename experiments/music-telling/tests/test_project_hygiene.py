"""The Makefile, compose file and Dockerfile keep the monorepo's rules: no volume
deletion, local-only ports, pinned images, a locked uv env that never touches
another venv, and the standard make targets."""

import os
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def make_n(target: str) -> str:
    env = {**os.environ, "VIRTUAL_ENV": "/nonexistent/foreign-venv"}
    out = subprocess.run(
        ["make", "-n", "-C", str(ROOT), target], capture_output=True, text=True, env=env
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_down_keeps_volumes():
    assert not re.search(r"down\b.*\s-v\b", make_n("down"))


def test_compose_is_named_local_and_pinned():
    assert not (ROOT / "docker-compose.yml").exists()
    compose = (ROOT / "compose.yaml").read_text()
    assert re.search(r"^name: music-telling$", compose, re.M)
    assert ":latest" not in compose
    for port in re.findall(r'^\s+- "([^"]+)"$', compose.split("ports:", 1)[1], re.M)[:1]:
        assert port.startswith("127.0.0.1:"), port
    assert "./src:" not in compose  # Colima can't see bind mounts


def test_dockerfile_pinned_and_copies_src_before_install():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert ":latest" not in dockerfile
    assert "||" not in dockerfile  # no silent fallback that drops mood support
    for stage in dockerfile.split("FROM base AS ")[1:]:
        assert stage.index("COPY src") < stage.index("uv sync"), stage


def test_uv_is_locked_and_project_scoped():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["requires-python"] == ">=3.12"
    assert (ROOT / "uv.lock").exists()
    assert "uv.lock" not in (ROOT / ".gitignore").read_text().split()
    for target in ("setup", "test", "lint"):
        cmds = make_n(target)
        assert "uv pip" not in cmds and "--locked" in cmds, cmds
    # The project venv is pinned, so an inherited VIRTUAL_ENV is never used.
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(r"^export UV_PROJECT_ENVIRONMENT\s*:?=", makefile, re.M)


def test_standard_targets_exist():
    for target in ("vuln", "secrets", "imports"):
        make_n(target)
