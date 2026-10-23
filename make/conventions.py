#!/usr/bin/env python3
"""`make conventions`: every project Makefile has the standard targets, and a workflow under .github/workflows
runs it (`make -C <project>`, or a job whose working-directory is the project). Prints the gaps; exits 1 on a
gap only with --strict. `--self-test` checks itself. Standard library only.

usage: conventions.py [--strict] PROJECT... | --self-test"""

import re
import sys
import tempfile
from pathlib import Path

TARGETS = ("test", "lint", "imports", "vuln", "secrets", "up", "down", "hygiene")


def gaps(root: Path, project: str) -> list[str]:
    makefile = root / project / "Makefile"
    if not makefile.exists():
        return [f"{project}: no Makefile"]
    defined = {t for line in re.findall(r"(?m)^([\w -]+):(?!=)", makefile.read_text()) for t in line.split()}
    found = [f"{project}: no `{t}` target" for t in TARGETS if t not in defined]
    workflows = [p.read_text() for p in sorted((root / ".github/workflows").glob("*.y*ml"))]
    if project == ".":  # the root: a job that runs make with no working-directory
        ran = any(re.search(r"(?m)^[\s-]+run: make\b", w) and "working-directory:" not in w for w in workflows)
    else:
        ran = any(f"make -C {project}" in w or re.search(rf"(?m)^\s+working-directory: {re.escape(project)}/?$", w)
                  for w in workflows)
    return found + ([] if ran else [f"{project}: no workflow runs it"])


def self_test() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".github/workflows").mkdir(parents=True)
        (root / "a").mkdir()
        (root / "b").mkdir()
        (root / "Makefile").write_text("X := 1\ntest lint: x\nimports vuln secrets:\nup: deploy\ndown:\nhygiene:\n")
        (root / "a/Makefile").write_text("test:\n\ttrue\n")
        (root / ".github/workflows/ci.yml").write_text("jobs:\n  x:\n    steps:\n      - run: make hygiene\n")
        assert gaps(root, ".") == [], gaps(root, ".")
        assert gaps(root, "a") == [f"a: no `{t}` target" for t in TARGETS[1:]] + ["a: no workflow runs it"]
        assert gaps(root, "b") == ["b: no Makefile"]
        (root / ".github/workflows/a.yml").write_text("      - run: make -C a test\n")
        assert gaps(root, "a")[-1] != "a: no workflow runs it"
        (root / ".github/workflows/a.yml").write_text("    working-directory: a/sub\n")
        assert gaps(root, "a")[-1] == "a: no workflow runs it"  # below the project is not the project
    print("conventions self-test: ok")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--self-test"]:
        sys.exit(self_test())
    strict = "--strict" in args
    projects = [a for a in args if a != "--strict"]
    if not projects:
        sys.exit(__doc__.rsplit("\n", 1)[-1])
    found = [gap for p in projects for gap in gaps(Path.cwd(), p)]
    print("\n".join(found) if found else "conventions: every project meets them")
    sys.exit(1 if strict and found else 0)
