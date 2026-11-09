"""Measure how head pose and body yaw combine on the real robot: commanded vs read back.

    REACHY_MINI_HOST=<robot> uv run python robot/calibration/frames_probe.py

The bot stopped, the robot awake.
"""
import math
import os
import time

import numpy as np
from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

CASES = [  # (head yaw deg, head pitch deg, body yaw deg)
    (0, 0, 0), (40, 0, 0), (0, 0, 60), (60, 0, 60), (100, 0, 60), (150, 0, 120),
    (175, 0, 150), (-60, 0, -60), (-175, 0, -150), (0, 15, 0), (0, -25, 0), (60, -25, 60), (0, 0, 0)]


HOST = os.environ["REACHY_MINI_HOST"]


def yaw_pitch(pose):
    r = np.asarray(pose)[:3, :3]
    return math.degrees(math.atan2(r[1, 0], r[0, 0])), math.degrees(math.asin(-max(-1, min(1, r[2, 0]))))


with ReachyMini(host=HOST, connection_mode="network") as mini:
    mini.enable_motors()
    print(f"{'cmd head yaw/pitch':>18} {'cmd body':>8} | {'got head yaw/pitch (world)':>26} {'got body':>8} {'rel':>6}")
    for hy, hp, by in CASES:
        mini.goto_target(create_head_pose(yaw=hy, pitch=hp), body_yaw=math.radians(by), duration=1.2)
        time.sleep(0.4)
        gy, gp = yaw_pitch(mini.get_current_head_pose())
        body = math.degrees(mini.get_current_joint_positions()[0][0])
        print(f"{hy:>9}/{hp:<8} {by:>8} | {gy:>17.1f}/{gp:<8.1f} {body:>8.1f} {gy - body:>6.1f}")
