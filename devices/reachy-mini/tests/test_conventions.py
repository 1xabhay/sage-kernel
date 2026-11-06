"""Every project Makefile under devices/ has the owner's standard targets, and CI runs them.

Stdlib only: the workflow is read line by line, not as YAML.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/reachy.yml"
STANDARD = {"test", "lint", "imports", "vuln", "secrets", "up", "down", "hygiene"}
# `hygiene` is these, and CI runs them, except `secrets`: the root CI job scans the whole repo.
HYGIENE = ["lint", "imports", "vuln", "secrets", "test"]
IN_CI = [t for t in HYGIENE if t != "secrets"]
# The voice app and its services are the VOICE lane's; strict, so this fails once they comply.
VOICE = "devices/reachy-mini/apps/reachy_sage_voice"
PROJECTS = [
    pytest.param(
        p,
        marks=pytest.mark.xfail(strict=True, reason="VOICE lane adds the targets and the CI job"),
    )
    if p.startswith(VOICE)
    else p
    for p in sorted(
        str(Path(f).parent)
        for f in subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "devices/*/Makefile"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )
]


def targets(makefile: Path) -> dict[str, list[str]]:
    """Each rule's name, mapped to its prerequisites."""
    rules = {}
    for line in makefile.read_text().splitlines():
        m = re.match(r"^([A-Za-z][\w-]*(?: [A-Za-z][\w-]*)*):(?!=)(.*)$", line)
        if m:
            for name in m[1].split():
                rules[name] = m[2].split()
    return rules


def ci_makes() -> dict[str, set[str]]:
    """Each job's working directory, mapped to the targets its `make` steps run."""
    jobs: dict[str, set[str]] = {}
    workdir = None
    for line in WORKFLOW.read_text().splitlines():
        if re.match(r"^  [\w-]+:\s*$", line):
            workdir = None
        elif m := re.match(r"^\s+working-directory:\s*(\S+)", line):
            workdir = m[1]
            jobs.setdefault(workdir, set())
        elif workdir and (m := re.match(r"^\s+(?:- )?run:\s*make\s+(.*)$", line)):
            jobs[workdir] |= {w for w in m[1].split() if not w.startswith("-")}
    return jobs


def test_found_the_projects():
    assert "devices/reachy-mini" in [p if isinstance(p, str) else p.values[0] for p in PROJECTS]


@pytest.mark.parametrize("project", PROJECTS)
def test_makefile_has_the_standard_targets(project):
    rules = targets(ROOT / project / "Makefile")
    assert rules.keys() >= STANDARD, f"missing: {sorted(STANDARD - rules.keys())}"
    assert rules["hygiene"] == HYGIENE


@pytest.mark.parametrize("project", PROJECTS)
def test_ci_runs_the_hygiene_targets(project):
    ran = ci_makes().get(project, set())
    assert set(IN_CI) <= ran, f"reachy.yml never runs make {sorted(set(IN_CI) - ran)} in {project}"


def test_targets_parser(tmp_path):
    mk = tmp_path / "Makefile"
    mk.write_text("X := 1\nup down:\n\techo\nhygiene: lint test\n.PHONY: up\n")
    assert targets(mk) == {"up": [], "down": [], "hygiene": ["lint", "test"]}
