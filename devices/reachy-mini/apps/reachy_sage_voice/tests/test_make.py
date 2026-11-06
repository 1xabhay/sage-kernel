"""The app's and its services' Makefiles have the owner's standard targets.

Hygiene is exactly the gates; devices/reachy-mini's tests/test_conventions.py checks the same
once PR #10 lands.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
STANDARD = {"test", "lint", "imports", "vuln", "secrets", "up", "down", "hygiene"}
HYGIENE = ["lint", "imports", "vuln", "secrets", "test"]


def targets(makefile: Path) -> dict[str, list[str]]:
    """Each rule's name, mapped to its prerequisites (test_conventions.py's parser)."""
    rules = {}
    for line in makefile.read_text().splitlines():
        m = re.match(r"^([A-Za-z][\w-]*(?: [A-Za-z][\w-]*)*):(?!=)(.*)$", line)
        if m:
            for name in m[1].split():
                rules[name] = m[2].split()
    return rules


@pytest.mark.parametrize("project", [".", "services"])
def test_makefile_has_the_standard_targets(project):
    rules = targets(APP / project / "Makefile")
    assert rules.keys() >= STANDARD, f"missing: {sorted(STANDARD - rules.keys())}"
    assert rules["hygiene"] == HYGIENE


@pytest.mark.parametrize("has_it, starts", [(True, "caffeinate -i -s uv run"), (False, "uv run")])
def test_run_keeps_the_mac_awake_only_where_caffeinate_exists(tmp_path, has_it, starts):
    # On Linux the recipe began "-i -s uv run": the flags, without the command they belong to.
    if has_it:
        tool = tmp_path / "caffeinate"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
    ran = subprocess.run([shutil.which("make"), "-n", "run"], cwd=APP, capture_output=True,
                         text=True, env={"PATH": str(tmp_path), "HOME": str(tmp_path)}, timeout=60)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip().startswith(starts)


def test_down_keeps_the_volumes():
    for project in (".", "services"):
        recipes = [line for line in (APP / project / "Makefile").read_text().splitlines()
                   if line.startswith("\t")]
        assert not [line for line in recipes if re.search(r"down\b.*(-v\b|--volumes)", line)]
