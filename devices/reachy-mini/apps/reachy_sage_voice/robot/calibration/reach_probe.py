"""Where does the body actually stop, each way? Commanded body yaw vs reached, head turning with it.

    REACHY_MINI_HOST=<robot> uv run python robot/calibration/reach_probe.py   (bot stopped)
"""
import math
import os
import time

from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

HOST = os.environ["REACHY_MINI_HOST"]


with ReachyMini(host=HOST, connection_mode="network") as mini:
    mini.stop_head_tracking()
    for target in (120, 140, 150, 160, -120, -130, -140, -150, -160):
        mini.goto_target(create_head_pose(), body_yaw=0.0, duration=1.5)
        mini.goto_target(create_head_pose(yaw=target), body_yaw=math.radians(target), duration=2.0)
        time.sleep(0.8)
        body = math.degrees(mini.get_current_joint_positions()[0][0])
        print(f"body cmd {target:5d} -> reached {body:7.1f} (short by {target - body:5.1f})")
    mini.goto_target(create_head_pose(), body_yaw=0.0, duration=1.5)
