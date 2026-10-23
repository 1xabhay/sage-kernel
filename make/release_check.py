#!/usr/bin/env python3
"""`make release`: HEAD carries tag kernel/vX.Y.Z, X.Y.Z is pyproject.toml's version, and CHANGELOG.md has a
`## [X.Y.Z]` section. Checks only; it never tags or pushes. `--self-test` checks itself. Standard library only."""

import re
import subprocess
import sys
import tomllib
from pathlib import Path


def problems(tags: list[str], pyproject: str, changelog: str) -> list[str]:
    version = tomllib.loads(pyproject)["project"]["version"]
    found = []
    if f"kernel/v{version}" not in tags:
        found.append(f"HEAD is not tagged kernel/v{version} (its kernel tags: {[t for t in tags if t.startswith('kernel/')]})")
    if not re.search(rf"(?m)^## \[{re.escape(version)}\]", changelog):
        found.append(f"CHANGELOG.md has no ## [{version}] section")
    return found


def self_test() -> None:
    py, log = '[project]\nversion = "1.2.3"\n', "## [Unreleased]\n\n## [1.2.3] - 2026-10-23\n"
    assert problems(["kernel/v1.2.3", "v0.1.0"], py, log) == []
    assert len(problems(["kernel/v1.2.2"], py, log)) == 1
    assert len(problems([], py, "## [1.2.30]\n")) == 2
    print("release_check self-test: ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    tags = subprocess.run(["git", "tag", "--points-at", "HEAD"], check=True, capture_output=True, text=True).stdout
    found = problems(tags.split(), Path("pyproject.toml").read_text(), Path("CHANGELOG.md").read_text())
    print("\n".join(found), file=sys.stderr) if found else print("release: HEAD is ready to publish")
    sys.exit(1 if found else 0)
