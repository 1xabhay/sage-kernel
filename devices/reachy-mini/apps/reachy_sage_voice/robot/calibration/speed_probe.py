"""Can the body keep up with the head? A 60 deg turn of both at several durations, sampled mid-move.

    REACHY_MINI_HOST=<robot> uv run python robot/calibration/speed_probe.py   (bot stopped)
"""
import math
import os
import threading
import time

import numpy as np
from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

HOST = os.environ["REACHY_MINI_HOST"]


def world_yaw(pose):
    r = np.asarray(pose)[:3, :3]
    return math.degrees(math.atan2(r[1, 0], r[0, 0]))


with ReachyMini(host=HOST, connection_mode="network") as mini:
    mini.stop_head_tracking()
    for duration in (0.6, 1.0, 1.5):
        mini.goto_target(create_head_pose(), body_yaw=0.0, duration=1.5)
        time.sleep(0.5)
        worst, samples = 0.0, []
        t = threading.Thread(target=mini.goto_target, args=(create_head_pose(yaw=60),),
                             kwargs={"body_yaw": math.radians(60), "duration": duration})
        t.start()
        start = time.time()
        while time.time() - start < duration + 0.8:
            head = world_yaw(mini.get_current_head_pose())
            body = math.degrees(mini.get_current_joint_positions()[0][0])
            samples.append((round(time.time() - start, 2), round(head), round(body)))
            worst = max(worst, abs(head - body))
            time.sleep(0.08)
        t.join()
        print(f"duration {duration}s: worst |head-body| {worst:5.1f} deg; end head/body {samples[-1][1]}/{samples[-1][2]}; "
              f"mid {samples[len(samples)//3]}")
    mini.goto_target(create_head_pose(), body_yaw=0.0, duration=1.5)
