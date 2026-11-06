"""Run the real search on the robot and record commanded vs actual head/body angles.

    REACHY_MINI_HOST=<robot> uv run python robot/calibration/search_probe.py   (bot stopped)
"""
import asyncio
import math
import os
import threading
import time

import numpy as np
from reachy_mini import ReachyMini

from reachy_sage_voice import grammar
from reachy_sage_voice.executor import Executor, daemon_doa, daemon_face

HOST = os.environ["REACHY_MINI_HOST"]


def world_yaw(pose):
    r = np.asarray(pose)[:3, :3]
    return math.degrees(math.atan2(r[1, 0], r[0, 0]))


with ReachyMini(host=HOST, connection_mode="network") as mini:
    samples, stop = [], threading.Event()

    def sample():
        while not stop.is_set():
            try:
                head = world_yaw(mini.get_current_head_pose())
                body = math.degrees(mini.get_current_joint_positions()[0][0])
                samples.append((time.time(), head, body))
            except Exception as error:
                samples.append((time.time(), None, str(error)))
            time.sleep(0.1)

    threading.Thread(target=sample, daemon=True).start()
    import sys
    live = "--live" in sys.argv  # as in the bot: idle motion and cues on, a glance and a thought first
    executor = Executor(mini, doa=daemon_doa(f"http://{HOST}:8000/api/state/doa"),
                        face=daemon_face(f"http://{HOST}:8000/api/media/tracking/face"),
                        idle=live, cues=live)
    plan = grammar.plan_for("Sage, look for me")
    t0 = time.time()

    async def go():
        if live:
            idle = asyncio.ensure_future(executor.breathe())
            await asyncio.sleep(2.0)
            executor.attend()  # the person starts speaking
            await asyncio.sleep(1.2)
            executor.resume()
            executor.think()  # the turn ended; the command arrives during the thought
            await asyncio.sleep(0.5)
        result = await executor.run(plan)
        return result
    result = asyncio.run(go())
    stop.set()
    print("result:", result, f"in {time.time() - t0:.1f}s")
    good = [(t, h, b) for t, h, b in samples if h is not None]
    worst = max(good, key=lambda s: abs(s[1] - s[2]))
    print(f"samples {len(good)}; worst |head-body| {abs(worst[1]-worst[2]):.1f} at t={worst[0]-t0:.1f}s (head {worst[1]:.0f}, body {worst[2]:.0f})")
    print(f"body range {min(b for _,_,b in good):.0f}..{max(b for _,_,b in good):.0f}; head range {min(h for _,h,_ in good):.0f}..{max(h for _,h,_ in good):.0f}")
    for t, h, b in good[::8]:
        print(f"  t={t - t0:5.1f}s head {h:7.1f} body {b:7.1f} rel {h - b:6.1f}")
