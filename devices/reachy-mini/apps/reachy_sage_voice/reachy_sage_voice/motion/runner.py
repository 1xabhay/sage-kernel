"""The 50 Hz motion runner: ticks the arbiter and sends its target (LAYERS.md §5 L5).

Effects are injected (clock, sleep, send), so a fake clock tests it. A late tick is dropped,
never queued: the motion is a function of the clock, so it carries on where it should be.
While the link is down it keeps the desired state and only probes, so it sees the link back.
Step 2: idle and the gotos (look, turn, home), one runner for the executor's life. Later: M1,
the single writer (emotes, dance, search as primaries, tracking weight owned); M2, the live
tick rate gate on the robot.
"""

import contextvars
import logging
import math
import statistics
from collections import Counter
from dataclasses import replace

from reachy_mini.utils import create_head_pose
from scipy.spatial.transform import Rotation

from reachy_sage_voice.limits import SDK_HEAD_YAW
from reachy_sage_voice.motion import alive
from reachy_sage_voice.motion.arbiter import (
    SETTLE_POS,
    STALL_S,
    State,
    rebased,
    still_s,
    sudden,
    tick,
)

logger = logging.getLogger(__name__)
DT = 0.02  # 50 Hz. ponytail: 0.04 (25 Hz) if the live link stalls (LAYERS.md risks)
#: True only inside the runner's send: the one writer (M1). Fakes check it.
SENDING = contextvars.ContextVar("sending", default=False)
PROBE_S = 0.5  # while the link is down, one send this often
SUDDEN_EVERY_S = 1.0  # the most often one sudden move line is logged, so a line is about one move
GATES_S = 60.0  # one "motion gates" line this often: M2's rate, A5's clamps, A6's still (LAYERS §4)


def _gates() -> dict:
    return {"since": None, "ticks": 0, "late": 0, "clamped": 0, "axes": Counter(), "sudden": 0,
            "gaps": [], "still": 0.0, "from": None}


def _count(g: dict, t: float, was: State, now: State, fast) -> None:
    """Add one tick to the minute's gates, and log the line once the minute is up."""
    g["since"] = t if g["since"] is None else g["since"]
    gap = None if was.t is None else t - was.t
    if gap is not None and gap > 0:
        g["late"] += gap > 1.5 * DT
        if gap <= STALL_S:  # past it the runner had yielded, or the link was down
            g["gaps"].append(gap)
    g["ticks"] += 1
    g["clamped"] += bool(now.clamped)
    g["axes"].update(now.clamped)
    g["sudden"] += fast is not None
    held = (gap is not None and 0 < gap <= STALL_S and was.sent is not None
            and all(abs(v - was.sent.get(k, v)) <= SETTLE_POS for k, v in now.sent.items()))
    g["from"] = (was.t if g["from"] is None else g["from"]) if held else None
    run = t - g["from"] if held else 0.0  # the target unchanged since: A6's still stretch
    g["still"] = max(g["still"], run)
    if t - g["since"] >= GATES_S:
        gaps = g["gaps"]
        hz = len(gaps) / sum(gaps) if gaps else 0.0
        off = [abs(x - DT) for x in gaps]
        jitter = statistics.quantiles(off, n=20)[-1] if len(off) > 1 else 0.0
        axes = " ".join(f"{k} {n}" for k, n in g["axes"].most_common())
        logger.info("motion gates: %d ticks, %.1f Hz, late %d, jitter p95 %d ms, clamped %d%s, "
                    "sudden %d, still max %.1f s", g["ticks"], hz, g["late"], jitter * 1000,
                    g["clamped"], f" ({axes})" if axes else "", g["sudden"], g["still"])
        kept = g["from"]  # a still stretch goes on into the next minute
        g.update(_gates(), since=t, still=run)
        g["from"] = kept


def idle(primary, t0: float, scale: float = 1.0, speaking=lambda: False, speed: float = 1.0,
         reflex=lambda t: {}):
    """Return ``step(t)``: ``primary()`` with breathing and drift under it, fading in from ``t0``.

    A new primary counts as a move: the fade starts again from it. The antenna clock stops
    while ``speaking()`` (the listening freeze). ``speed`` scales the slew (``arbiter.tick``).
    ``reflex(t)`` is one more offset, over any primary (``alive.listening``).
    """
    state, antenna_t, last, held = State(moved_at=t0), 0.0, t0, None
    warned, gates = -math.inf, _gates()

    def step(t: float) -> dict:
        nonlocal state, antenna_t, last, held, warned
        antenna_t, last = antenna_t + (0.0 if speaking() else t - last), t
        if (now := primary()) is not held:
            # the jump between primaries is not the goal moving: the slew brakes or speeds up
            # from its own speed, not one more tick at it (a stop's Hold lands, never comes back)
            state, held = replace(state, moved_at=t, goal={}), now
        offsets = alive.offsets(t, still_s(state, t, held), antenna_t, scale)
        was = state
        state, target = tick(state, t, held, (*offsets, reflex(t)), speed)
        if (fast := sudden(was, state, speed)) and t - warned >= SUDDEN_EVERY_S:
            warned = t
            logger.warning("sudden move: %s %.4g/s, limit %.4g", *fast)
        _count(gates, t, was, state, fast)
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
    """Return ``set_target`` arguments for an arbiter target, never where it would drag the body."""
    body = target["body_yaw"]
    head = create_head_pose(target.get("x", 0.0), target.get("y", 0.0), target["z"],
                            target["roll"], target["pitch"],
                            max(body - SDK_HEAD_YAW, min(body + SDK_HEAD_YAW, target["yaw"])))
    return {"head": head, "antennas": [math.radians(target["left"]), math.radians(target["right"])],
            "body_yaw": math.radians(target["body_yaw"])}
