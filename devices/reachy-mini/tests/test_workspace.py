"""The workspace stays coherent: its members exist and no device speaks protocol v0."""

import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V0 = "/v" + "0/"  # split so this file does not match itself


def test_no_tracked_file_under_devices_speaks_protocol_v0():
    files = subprocess.run(
        ["git", "ls-files", "-z", "--", "."],
        cwd=ROOT.parent,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    v0 = V0.encode()
    hits = [f.decode() for f in files if f and v0 in (ROOT.parent / f.decode()).read_bytes()]
    assert hits == []


def test_workspace_sources_exist():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    for name in project["tool"]["uv"]["sources"]:
        app = ROOT / "apps" / name.replace("-", "_")
        assert (app / "pyproject.toml").is_file(), app
