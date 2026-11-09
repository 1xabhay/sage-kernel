"""Does the daemon's face tracker fight scripted head moves? Same goto with tracking off, then on.

    REACHY_MINI_HOST=<robot> uv run python robot/calibration/tracking_probe.py   (bot stopped)
"""
import json
import math
import os
import time
import urllib.request

import numpy as np
from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

HOST = os.environ["REACHY_MINI_HOST"]
FACE = f"http://{HOST}:8000/api/media/tracking/face"


def world_yaw(pose):
    r = np.asarray(pose)[:3, :3]
    return math.degrees(math.atan2(r[1, 0], r[0, 0]))


def face():
    try:
        return json.loads(urllib.request.urlopen(FACE, timeout=2).read() or b"null")
    except Exception as error:
        return f"error {error}"


with ReachyMini(host=HOST, connection_mode="network") as mini:
    for label, weight in (("tracking off", None), ("tracking 0.05", 0.05), ("tracking 1.0", 1.0)):
        mini.stop_head_tracking()
        mini.goto_target(create_head_pose(), body_yaw=0.0, duration=1.0)
        if weight is not None:
            mini.start_head_tracking(weight=weight)
            time.sleep(1.0)
        mini.goto_target(create_head_pose(yaw=60), body_yaw=math.radians(60), duration=1.2)
        time.sleep(1.0)
        head = world_yaw(mini.get_current_head_pose())
        body = math.degrees(mini.get_current_joint_positions()[0][0])
        print(f"{label:>14}: commanded head 60 body 60 -> head {head:6.1f} body {body:6.1f} rel {head - body:6.1f} | face {face()}")
    mini.stop_head_tracking()
    mini.goto_target(create_head_pose(), body_yaw=0.0, duration=1.0)
