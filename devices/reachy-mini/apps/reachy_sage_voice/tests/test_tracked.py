"""Git tracks no audio, arrays, .env files or voiceprints in this app.

Recordings (*.wav), embeddings (*.npy), .env files and anything under
services/profiles/ are biometric or secret: local only, never committed. The one
exception is named below with why.
"""

import pathlib
import subprocess

import pytest

APP = pathlib.Path(__file__).parents[1]
ALLOWED = {
    # Keeps the folder and ignores everything else in it.
    "services/profiles/.gitignore",
    # Synthetic: Kokoro's voice, no person's (NOTICE). The STT service's tests transcribe it.
    "services/stt/fixtures/good-morning-little-robot.wav",
}


def forbidden(paths: list[str]) -> list[str]:
    """Return the paths in ``paths`` that must not be tracked."""
    return [path for path in paths if path not in ALLOWED and (
        path.endswith((".wav", ".npy")) or pathlib.PurePosixPath(path).name == ".env"
        or path.startswith("services/profiles/"))]


def tracked() -> list[str]:
    """Files git tracks under the app folder, relative to it."""
    try:
        listing = subprocess.run(["git", "ls-files", "-z"], cwd=APP, check=True,
                                 capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        # make test-docker streams the files in without .git; the host run and the hook check.
        pytest.skip("not in a git checkout")
    return [path for path in listing.split("\0") if path]


def test_git_tracks_nothing_forbidden():
    paths = tracked()
    assert "pyproject.toml" in paths
    assert forbidden(paths) == []


def test_each_forbidden_kind_is_caught():
    planted = ["eval/recordings/owner-01.wav", "services/stt/owner.npy", ".env",
               "services/.env", "services/profiles/owner.json", "README.md",
               "services/profiles/.gitignore", "services/.env.example"]
    assert forbidden(planted) == planted[:5]


def test_the_commit_gate_and_the_secret_scan_are_the_repos_not_the_apps():
    # The root's gate covers every app; this app's copy named a path the monorepo doesn't have.
    assert not any(path.startswith(".githooks/") for path in tracked())
    make = (APP / "Makefile").read_text()
    assert "\nhooks:" not in make and "core.hooksPath" not in make
    recipe = make.split("\nsecrets:\n", 1)[1].split("\n\n", 1)[0]
    assert recipe.strip() == "$(MAKE) -C $(ROOT) secrets"
