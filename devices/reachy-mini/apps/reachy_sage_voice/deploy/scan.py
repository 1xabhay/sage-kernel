"""`make release`: no file in a folder names a LAN address, a hardware address or a home folder.

The patterns are the app's own (tests/test_hygiene.py) and, when this checkout has it, the root's
.githooks/hygiene.py. Prints each hit and exits 1. Stdlib only: `python3 -I deploy/scan.py <dir>`.
"""

import importlib.util
import sys
import types
from pathlib import Path

APP = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    """Import a module from a file."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(folder: Path) -> int:
    """Print each hit under folder; return 1 if any."""
    sys.modules.setdefault("pytest", types.ModuleType("pytest"))  # test_hygiene imports it to skip
    checks = [load("app_hygiene", APP / "tests/test_hygiene.py").findings]
    root = APP.parents[3] / ".githooks/hygiene.py"
    if root.is_file():
        pattern = load("root_hygiene", root).PATTERN
        checks.append(lambda text: [m.group(0) for m in pattern.finditer(text)])
    bad = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue  # binary
        bad += [f"{path.relative_to(folder)}: {hit}" for check in checks for hit in check(text)]
    for line in bad:
        print(f"release: names an address or a home folder: {line}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
