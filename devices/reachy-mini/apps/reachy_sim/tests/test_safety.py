"""Nothing here may build an SDK client that could find the real robot."""

import ast
import re
import sys
from pathlib import Path

import pytest
import reachy_mini.daemon.app.main
from reachy_mini.utils.discovery import MdnsServiceRegistration

import sim

APP = Path(__file__).parent.parent


def unsafe_clients(source: str) -> list[int]:
    """Return the lines that construct ReachyMini without connection_mode="localhost_only"."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "ReachyMini":
            continue
        mode = next((k.value for k in node.keywords if k.arg == "connection_mode"), None)
        if not (isinstance(mode, ast.Constant) and mode.value == "localhost_only"):
            lines.append(node.lineno)
    return lines


def test_no_code_in_the_app_builds_a_client_that_could_leave_localhost():
    sources = [p for p in APP.rglob("*.py") if ".venv" not in p.parts]
    assert sources
    found = {str(p): unsafe_clients(p.read_text()) for p in sources}
    assert not {p: lines for p, lines in found.items() if lines}


def test_the_check_catches_a_client_without_localhost_only():
    bad = "ReachyMini()\nreachy_mini.ReachyMini(connection_mode='network')"
    assert unsafe_clients(bad) == [1, 2]
    assert unsafe_clients("ReachyMini(port=8742, connection_mode='localhost_only')") == []


def test_the_sim_daemon_listens_on_loopback_only_and_never_registers_on_mdns(monkeypatch):
    """The stock daemon announces itself on the LAN: a sim must never be found as a robot."""
    started = []
    announce = MdnsServiceRegistration.register
    monkeypatch.setattr(
        MdnsServiceRegistration, "register", announce
    )  # put back after sim.main's own patch
    monkeypatch.setattr(sys, "argv", sys.argv)
    monkeypatch.setenv("HF_HUB_OFFLINE", "")
    monkeypatch.setattr(reachy_mini.daemon.app.main, "main", lambda: started.append(list(sys.argv)))

    sim.main("8744")

    (argv,) = started
    assert {"--sim", "--headless", "--no-media"} <= set(argv)
    assert argv[argv.index("--fastapi-host") + 1] == "127.0.0.1"
    assert argv[argv.index("--fastapi-port") + 1] == "8744"
    assert MdnsServiceRegistration.register is not announce
    assert MdnsServiceRegistration.register(object()) is None  # a registration that does nothing


@pytest.mark.xfail(
    strict=True, reason="the sim pins reachy-mini 1.10.0, the voice app 1.14.0: bump the sim"
)
def test_the_sim_runs_the_sdk_version_the_voice_app_pins():
    """The limits measured here speak for the live body only on the daemon it runs."""

    def pin(project):
        return re.search(
            r'"reachy-mini(?:\[\w+\])?==([\d.]+)"', (project / "pyproject.toml").read_text()
        )[1]

    assert pin(APP) == pin(APP.parent / "reachy_sage_voice")
