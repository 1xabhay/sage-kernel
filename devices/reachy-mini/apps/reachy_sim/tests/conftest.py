"""One sim daemon for the session, on localhost, on a port of its own."""

import json
import math
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

# Not 8000 (the SDK default) and not 8742 (`make sim`), so tests run beside either.
PORT = 8743
API = f"http://127.0.0.1:{PORT}/api"
APP = Path(__file__).parent.parent


def api(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def pose(roll=0.0, pitch=0.0, yaw=0.0):
    """Build a head pose from degrees, in the radians the REST API wants."""
    rad = {"roll": roll, "pitch": pitch, "yaw": yaw}
    return {"x": 0, "y": 0, "z": 0} | {k: math.radians(v) for k, v in rad.items()}


def present():
    """Read the state back, in degrees, plus raw joints for rendering."""
    s = api("/state/full?with_head_joints=true&with_body_yaw=true")
    deg = {k: math.degrees(s["head_pose"][k]) for k in ("roll", "pitch", "yaw")}
    return deg | {
        "body_yaw": math.degrees(s["body_yaw"]),
        "head_joints": s["head_joints"],
        "antennas": s["antennas_position"],
    }


ANGLES = ("roll", "pitch", "yaw", "body_yaw")


def still(within, step=0.2, calm=0.05):
    """Read the state once nothing turned over `calm` degrees in three reads, or at `within` s."""
    deadline, last, quiet = time.monotonic() + within, present(), 0
    while quiet < 3 and time.monotonic() < deadline:
        time.sleep(step)
        now = present()
        quiet = quiet + 1 if all(abs(now[k] - last[k]) < calm for k in ANGLES) else 0
        last = now
    return last


def reach(key, want, within=6.0, tolerance=2.0):
    """Read the state once `key` is within `tolerance` degrees of `want`, or after `within` s."""
    deadline = time.monotonic() + within
    while abs((now := present())[key] - want) >= tolerance and time.monotonic() < deadline:
        time.sleep(0.1)
    return now


def goto(settle=4.0, body_yaw=None, **head):
    """Move over 1 s, then wait out the actuators' slow tail: until still, at most `settle` s."""
    body = {"head_pose": pose(**head), "duration": 1.0}
    if body_yaw is not None:
        body["body_yaw"] = math.radians(body_yaw)
    api("/move/goto", body)
    time.sleep(1.0)  # the move itself
    return still(settle - 1.0)


@pytest.fixture(scope="session")
def sim():
    with tempfile.TemporaryFile() as log:  # the daemon's own words, shown only if it never comes up
        proc = subprocess.Popen(
            [sys.executable, str(APP / "sim.py"), str(PORT), "minimal"],
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
        try:
            for _ in range(120):
                try:
                    if api("/daemon/status")["state"] == "running":
                        break
                except OSError:
                    pass
                time.sleep(0.5)
            else:
                log.seek(0)
                pytest.fail(
                    "sim daemon did not come up:\n" + log.read().decode(errors="replace")[-4000:]
                )
            yield
        finally:
            proc.terminate()
            proc.wait(timeout=30)


@pytest.fixture
def neutral(sim):
    """Every test starts from head and body at zero."""
    return goto(body_yaw=0)
