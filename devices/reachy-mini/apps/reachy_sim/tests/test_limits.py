"""Commands beyond each documented limit (Pollen's docs) and what the sim actually reaches."""

import importlib.util
import math
import time
from pathlib import Path

import pytest

import limits

from conftest import api, goto, pose, present, reach

# (case, command in degrees, reached value to read, documented limit)
BEYOND = [
    ("head pitch +60", {"pitch": 60}, "pitch", 40),
    ("head pitch -60", {"pitch": -60}, "pitch", 40),
    ("head roll +60", {"roll": 60}, "roll", 40),
    ("head roll -60", {"roll": -60}, "roll", 40),
    ("head yaw +200 (wraps)", {"yaw": 200}, "yaw", 180),
    ("body yaw +200, head with it", {"yaw": 179, "body_yaw": 200}, "body_yaw", 180),
    ("body yaw -200, head with it", {"yaw": -179, "body_yaw": -200}, "body_yaw", 180),
    ("body yaw +170, head at 0", {"body_yaw": 170}, "body_yaw - head_yaw", 65),
]


def _voice_limits():
    """Load the live body's table: the voice app's limits.py, by path (it imports nothing)."""
    path = Path(__file__).parents[2] / "reachy_sage_voice" / "reachy_sage_voice" / "limits.py"
    spec = importlib.util.spec_from_file_location("voice_limits", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VOICE = _voice_limits()
# Each edge of the voice app's table, as a pose asked for well past it: (asked, value read, limit).
EDGES = (
    [
        pytest.param(
            {key: sign * 90.0}, key, VOICE.LIMITS[key][sign > 0], id=f"{key}{sign * 90:+.0f}"
        )
        for key in ("pitch", "roll")
        for sign in (-1, 1)
    ]
    + [
        pytest.param(
            {"yaw": sign * 200.0, "body_yaw": sign * 200.0},
            "body_yaw",
            VOICE.LIMITS["body_yaw"][sign > 0],
            id=f"body_yaw{sign * 200:+.0f}",
        )
        for sign in (-1, 1)
    ]
    + [
        pytest.param(
            {"yaw": sign * 90.0, "body_yaw": 0.0},
            "yaw",
            VOICE.LIMITS["head_yaw"][sign > 0],
            id=f"head_off_body{sign * 90:+.0f}",
        )
        for sign in (-1, 1)
    ]
)


def reached(state, key):
    if key == "body_yaw - head_yaw":
        return state["body_yaw"] - state["yaw"]
    return state[key]


def set_target(clamped):
    """Send one streamed target, as the voice app does: a refused one moves nothing."""
    head = {k: v for k, v in clamped.items() if k != "body_yaw"}
    body = {"target_head_pose": pose(**head)}
    if "body_yaw" in clamped:
        body["target_body_yaw"] = math.radians(clamped["body_yaw"])
    api("/move/set_target", body)


def test_commands_beyond_each_documented_limit_are_clamped(neutral):
    rows, failures = [], []
    for i, (case, command, key, limit) in enumerate(BEYOND):
        if i:
            goto(body_yaw=0)  # `neutral` did it for the first
        value = reached(goto(**command), key)
        asked = command.get(key, command.get("body_yaw"))
        rows.append(f"| {case} | {key} | ±{limit}° | {value:+.1f}° |")
        # Clamped: well short of the command. The sim's stops are not exactly the
        # documented ones (see README), so the documented limit gets 12° of slack
        # (roll measured up to 48.9°).
        if abs(value) > abs(asked) - 10 or abs(value) > limit + 12:
            failures.append(case)
    print("\n| command | reads | documented | reached |\n|---|---|---|---|", *rows, sep="\n")
    assert not failures, failures


def test_a_single_target_beyond_the_pitch_limit_is_dropped_not_clamped(neutral):
    """set_target (the streaming path apps use) has no trajectory to stop part-way."""
    set_target({"pitch": 60})
    deadline, worst = time.monotonic() + 1.5, 0.0
    while time.monotonic() < deadline:
        worst = max(worst, abs(present()["pitch"]))
        time.sleep(0.1)
    assert worst < 2


def test_the_app_clamps_pitch_and_roll_to_40_and_body_yaw_to_160():
    assert limits.clamp({"roll": -55.0, "pitch": 60.0, "yaw": 120.0, "body_yaw": 200.0}) == {
        "roll": -40.0,
        "pitch": 40.0,
        "yaw": 120.0,
        "body_yaw": 160.0,
    }
    assert limits.clamp({"pitch": -12.5}) == {"pitch": -12.5}
    assert limits.clamp({"body_yaw": -170.0}) == {"body_yaw": -160.0}


@pytest.mark.parametrize(("asked", "key", "limit"), EDGES)
def test_every_edge_of_the_voice_apps_limits_is_a_target_the_daemon_takes(
    neutral, asked, key, limit
):
    """Eval A5: the daemon drops a target past a limit, which reads as a freeze.

    A pose clamped by the live table (reachy_sage_voice/limits.py) must be one the daemon moves
    to, at every edge.
    """
    clamped = VOICE.clamp(asked)
    assert clamped[key] == limit
    set_target(clamped)
    assert reach(key, limit)[key] == pytest.approx(limit, abs=2.0)
