"""A ReachyMini stand-in: records calls, blocks in goto_target as the SDK does, scripts faces.

Like the SDK (research, 2026-10-23): goto_target's body_yaw defaults to 0.0 (it
turns the body home); head poses are in the world frame, and with automatic body
yaw a head more than 65° off the body drags the body after it; the daemon's
stop_move goes through ``client.send_command``. Head tracking as the daemon's: at a weight of
1 with a face to ``aim`` at, the head follows the aim and a set_target's head is dropped, while
its antennas and body yaw still land (reachy_mini daemon/backend/abstract.py:561-564, 602-604).
Below 1 the head blends the target and the aim. The dropped head is stored all the same, so weight
0 or a stop makes the last one live at motor speed (clear_tracking_aim): a jump in ``snaps``.
"""

import math
import os
import time
from types import SimpleNamespace

import numpy as np
from reachy_mini.utils.interpolation import linear_pose_interpolation

from reachy_sage_voice.motion import runner

MAX_RELATIVE_YAW = math.radians(65)
#: The limits a target must keep, in degrees (LAYERS.md §0, measured 2026-10-23, less 2): the
#: daemon silently drops one beyond them. Kept here by hand, so limits.py is checked, not copied.
BODY, LOOK, OFF_BODY, TILT = 158, (-155, 171), 53, 40


def head_yaw(head) -> float:
    return math.atan2(head[1][0], head[0][0])


def beyond(head, body_yaw) -> list[str]:
    """Return what of a target is past the limits (A5): body, look, tilt, or head off the body."""
    out = []
    if body_yaw is not None and abs(math.degrees(body_yaw)) > BODY + 1e-6:
        out.append(f"body {math.degrees(body_yaw):.1f}")
    if head is not None:
        yaw = math.degrees(head_yaw(head))
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, -head[2][0]))))
        roll = math.degrees(math.atan2(head[2][1], head[2][2]))
        if not LOOK[0] - 1e-6 <= yaw <= LOOK[1] + 1e-6:
            out.append(f"look {yaw:.1f}")
        if max(abs(pitch), abs(roll)) > TILT + 1e-6:
            out.append(f"tilt {pitch:.1f}/{roll:.1f}")
        if body_yaw is not None and abs(yaw - math.degrees(body_yaw)) > OFF_BODY + 1e-6:
            out.append(f"head {yaw - math.degrees(body_yaw):.1f} off the body")
    return out


#: Every motion call made off the runner's send path with the arbiter on (M1): conftest fails
#: the test on any. A test that means to break M1 clears it.
OFF_RUNNER: list[str] = []


def m1(name: str) -> None:
    """Record ``name`` as an M1 violation (LAYERS.md §4: one writer) unless the runner sends it."""
    if os.environ.get("SAGE_VOICE_ARBITER") == "1" and not runner.SENDING.get():
        OFF_RUNNER.append(name)


class FakeMini:
    def __init__(self, faces=(), age=0.0):
        """``faces`` scripts get_tracked_face().detected, one per call; False once used up.

        Each reading is stamped ``age`` seconds ago on ``time.monotonic``."""
        self.calls: list[tuple] = []
        self.age = age
        self.faces = list(faces)
        self.face_checks = 0
        self.yaw = 0.0  # body yaw, radians
        self.head = np.eye(4)
        self.antennas = [0.0, 0.0]  # radians, where a streamed target last put them
        self.dragged = 0  # moves where the head pulled the body off its target
        self.beyond: list[str] = []  # every target past the limits (A5): the daemon drops them
        self.weight: float | None = None  # the head tracking weight; None: off
        self.aim = None  # the head pose the daemon's tracking steers to; None: no face
        self.target = None  # the head last asked for: stored even while tracking drops it
        self.snaps: list[float] = []  # degrees the head jumped when tracking let go of it
        self.trail: list[tuple[float, float]] = []  # (time, yaw in degrees) of the head reached
        self.client = SimpleNamespace(
            send_command=lambda cmd: self.calls.append(("send_command", type(cmd).__name__)))
        self.media = SimpleNamespace(stop_playing=lambda: self.calls.append(("stop_playing",)))

    def goto_target(self, head=None, antennas=None, duration=0.5, method=None, body_yaw=0.0):
        assert antennas is None or len(antennas) == 2, f"the SDK takes 2 antennas, got {len(antennas)}"
        m1("goto_target")
        self.calls.append(("goto_target", time.monotonic(), body_yaw, head, antennas))
        self.beyond += beyond(head, body_yaw)
        time.sleep(duration)  # the SDK waits for the move, and cannot be cut short
        self.target = self.target if head is None else head
        self._land(head, body_yaw)

    def _land(self, head, body_yaw):
        """Reach a target, the head's and the body's (None keeps it)."""
        if head is not None:
            self.head = head
            self.trail.append((time.monotonic(), math.degrees(head_yaw(head))))
        if body_yaw is not None:  # the SDK's default 0.0 really does turn the body home
            self.yaw = body_yaw
        off = head_yaw(self.head) - self.yaw
        if abs(off) > MAX_RELATIVE_YAW + 1e-6:  # automatic body yaw: the body follows the head
            self.dragged += 1
            self.yaw = head_yaw(self.head) - math.copysign(MAX_RELATIVE_YAW, off)

    def set_target(self, head=None, antennas=None, body_yaw=None):
        assert antennas is None or len(antennas) == 2, f"the SDK takes 2 antennas, got {len(antennas)}"
        m1("set_target")
        self.calls.append(("set_target", time.monotonic(), head, body_yaw, antennas))
        self.antennas = self.antennas if antennas is None else list(antennas)
        self.beyond += beyond(head, body_yaw)
        self.target = self.target if head is None else head
        self._land(self._blended(head), body_yaw)  # a streamed target is reached by the next one

    def _blended(self, head):
        """Return the head the daemon steers to: the target, pulled ``weight`` toward the aim."""
        if self.aim is None or not self.weight:
            return head
        return linear_pose_interpolation(self.head if self.target is None else self.target,
                                         self.aim, min(1.0, self.weight))

    def _let_go(self):
        """Clear the aim (clear_tracking_aim): the stored target goes live, at motor speed."""
        if self.aim is not None and self.target is not None:
            self.snaps.append(abs(math.degrees(head_yaw(self.head) - head_yaw(self.target))))
            self._land(self.target, None)

    def start_head_tracking(self, weight=1.0):
        self.calls.append(("start_head_tracking", weight))
        if self.weight and not weight:
            self._let_go()
        self.weight = weight
        self._land(self._blended(None), None)

    def stop_head_tracking(self):
        self.calls.append(("stop_head_tracking",))
        if self.weight:
            self._let_go()
        self.weight = None

    def cancel_move(self):
        self.calls.append(("cancel_move",))

    def get_tracked_face(self, wait=True, timeout=5.0):
        self.face_checks += 1
        return SimpleNamespace(detected=self.faces.pop(0) if self.faces else False,
                               x=0.0, ts=time.monotonic() - self.age)

    def get_current_head_pose(self):
        return self.head

    def get_current_joint_positions(self):
        return [self.yaw, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], self.antennas

    def named(self, name):
        return [c for c in self.calls if c[0] == name]

    def moves(self):
        """Return every motion command in order, as (time, head, body_yaw, antennas).

        A goto_target, or a set_target: streamed by the motion runner (SAGE_VOICE_ARBITER=1),
        or a freeze.
        """
        return [(c[1], c[3], c[2], c[4]) if c[0] == "goto_target" else (c[1], c[2], c[3], c[4])
                for c in self.calls if c[0] in ("goto_target", "set_target")]
