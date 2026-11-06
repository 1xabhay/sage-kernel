"""The README (also the Hugging Face Space's page) points at the monorepo, names the kernel's
version, and its simulator demo never reaches the live kernel."""

import pathlib
import re
import tomllib

APP = pathlib.Path(__file__).resolve().parents[1]
ROOT = APP.parents[3]
README = (APP / "README.md").read_text()


def test_nothing_links_the_old_repo():
    for path in ("README.md", "pyproject.toml", "services/research/app.py"):
        assert "reachy-mini-lab" not in (APP / path).read_text(), path


def test_the_readme_names_the_kernels_version():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert re.findall(r"Sage kernel\]\([^)]*\) \(v([\d.]+)\)", README) == [version]


def test_the_simulator_demo_runs_a_throwaway_kernel():
    sim = README.split("## With the simulator", 1)[1].split("\n## ", 1)[0]
    assert "8776" not in sim and "KERNEL_TOKEN" not in sim
    assert "reachy_sim" in sim
