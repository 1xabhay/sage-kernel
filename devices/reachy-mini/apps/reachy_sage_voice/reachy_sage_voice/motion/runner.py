"""The 50 Hz motion runner: ticks the arbiter and sends its target (LAYERS.md §5 L5).

Effects are injected (clock, sleep, send), so a fake clock tests it. A late tick is dropped,
never queued: the motion is a function of the clock, so it carries on where it should be.
While the link is down it keeps the desired state and only probes, so it sees the link back.
Step 2: idle and the gotos (look, turn, home), one runner for the executor's life. Later: M1,
the single writer (emotes, dance, search as primaries, tracking weight owned); M2, the live
tick rate gate on the robot.
"""

import contextvars
import math
from dataclasses import replace

from reachy_mini.utils import create_head_pose
from scipy.spatial.transform import Rotation

from reachy_sage_voice.motion import alive
from reachy_sage_voice.motion.arbiter import State, rebased, still_s, tick

DT = 0.02  # 50 Hz. ponytail: 0.04 (25 Hz) if the live link stalls (LAYERS.md risks)
#: True only inside the runner's send: the one writer (M1). Fakes check it.
SENDING = contextvars.ContextVar("sending", default=False)
PROBE_S = 0.5  # while the link is down, one send this often


def idle(primary, t0: float, scale: float = 1.0, speaking=lambda: False, speed: float = 1.0,
         reflex=lambda t: {}):
    """Return ``step(t)``: ``primary()`` with breathing and drift under it, fading in from ``t0``.

    A new primary counts as a move: the fade starts again from it. The antenna clock stops
    while ``speaking()`` (the listening freeze). ``speed`` scales the slew (``arbiter.tick``).
    ``reflex(t)`` is one more offset, over any primary (``alive.listening``).
    """
    state, antenna_t, last, held = State(moved_at=t0), 0.0, t0, None

    def step(t: float) -> dict:
        nonlocal state, antenna_t, last, held
        antenna_t, last = antenna_t + (0.0 if speaking() else t - last), t
        if (now := primary()) is not held:
            # the jump between primaries is not the goal moving: the slew brakes or speeds up
            # from its own speed, not one more tick at it (a stop's Hold lands, never comes back)
            state, held = replace(state, moved_at=t, goal={}), now
        offsets = alive.offsets(t, still_s(state, t, held), antenna_t, scale)
        state, target = tick(state, t, held, (*offsets, reflex(t)), speed)
        step.state = state  # for the executor: a step is done once its move settles
        return target

    def rebase(pose: dict, t: float) -> None:
        nonlocal state
        state = step.state = rebased(state, pose, t)
    step.rebase = rebase
    return step


async def run(step, send, clock, sleep, busy=lambda: None, link_down=lambda: False,
              dt: float = DT) -> str:
    """Send ``step(t)`` every ``dt`` until ``busy()`` gives a reason, and return it.

    Nothing is sent once ``busy()`` says something else has the body.
    """
    next_t = probed = clock()
    probed -= PROBE_S
    while not (why := busy()):
        t = clock()
        down = link_down()
        if not down or t - probed >= PROBE_S:  # no step unsent: the slew starts from the last sent
            probed = t if down else probed
            await send(step(t))
        next_t += dt
        now = clock()
        if now >= next_t:  # late: skip the missed ticks, the next one a dt from now
            next_t = now + dt
        await sleep(next_t - now)
    return why


def pose(head, body_yaw: float, antennas) -> dict:
    """Return an arbiter pose (degrees, metres) from a 4x4 head pose and joints in radians."""
    roll, pitch, yaw = Rotation.from_matrix(head[:3, :3]).as_euler("xyz", degrees=True)
    x, y, z = head[:3, 3]
    return {"body_yaw": math.degrees(body_yaw), "yaw": yaw, "pitch": pitch, "roll": roll,
            "x": x, "y": y, "z": z, "left": math.degrees(antennas[0]),
            "right": math.degrees(antennas[1])}


def command(target: dict) -> dict:
    """Return ``set_target`` arguments for an arbiter target."""
    head = create_head_pose(target.get("x", 0.0), target.get("y", 0.0), target["z"],
                            target["roll"], target["pitch"], target["yaw"])
    return {"head": head, "antennas": [math.radians(target["left"]), math.radians(target["right"])],
            "body_yaw": math.radians(target["body_yaw"])}
