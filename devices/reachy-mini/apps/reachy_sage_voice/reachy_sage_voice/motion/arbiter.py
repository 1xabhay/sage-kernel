"""The motion arbiter's pure core: one primary source plus additive offsets, clamped.

Pollen's MovementManager pattern (reachy_mini_conversation_app, moves.py): at most one
primary (a goto, an emote or dance sample, a search view), offsets added on top, and the sum
clamped to ``limits.py`` (eval A5). Poses are dicts of degrees (body_yaw, yaw in the world,
pitch, roll, left and right antenna) and metres (z). No I/O and no threads: the 50 Hz writer
(LAYERS.md §5 L5) calls ``tick`` and sends the target.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from reachy_sage_voice.limits import LIMITS, clamp

NEUTRAL = {"body_yaw": 0.0, "yaw": 0.0, "pitch": 0.0, "roll": 0.0, "z": 0.0,
           "left": 0.0, "right": 0.0}
# ponytail: owner, 2026-10-27: hard starts and stops jerk the body, keep it slow; tune live
# (SAGE_VOICE_MOTION_SPEED). Per second, and per second squared: the sent target never
# changes faster (LAYERS.md §5 L5). A stop from VMAX coasts VMAX²/2AMAX: 7.5° for the body.
VMAX = {"body_yaw": 30.0, "yaw": 60.0, "pitch": 60.0, "roll": 60.0, "z": 0.03,
        "left": 200.0, "right": 200.0}
AMAX = {"body_yaw": 60.0, "yaw": 240.0, "pitch": 240.0, "roll": 240.0, "z": 0.3,
        "left": 1300.0, "right": 1300.0}


@dataclass(frozen=True)
class Hold:
    """A still primary, such as a search view or the tracking anchor: breathing runs under it."""

    pose: dict

    def at(self, t: float) -> dict:
        """Return the pose at ``t``."""
        return self.pose

    def moving(self, t: float) -> bool:
        """Whether this primary moves at ``t`` (aliveness waits for it)."""
        return False


@dataclass(frozen=True)
class Sample:
    """An emote or dance clip from ``t0`` for ``duration`` s: ``clip(s)`` is the pose ``s`` in."""

    clip: Callable[[float], dict]
    t0: float
    duration: float

    def at(self, t: float) -> dict:
        """Return the pose at ``t``."""
        return self.clip(max(0.0, min(t - self.t0, self.duration)))

    def moving(self, t: float) -> bool:
        """Whether this primary moves at ``t`` (aliveness waits for it)."""
        return t < self.t0 + self.duration


@dataclass(frozen=True)
class Goto:
    """A min-jerk move from ``start`` to ``end`` (its keys), from ``t0`` for ``duration`` s."""

    start: dict
    end: dict
    t0: float
    duration: float

    def at(self, t: float) -> dict:
        """Return the pose at ``t``."""
        u = max(0.0, min(1.0, (t - self.t0) / self.duration)) if self.duration > 0 else 1.0
        s = u ** 3 * (10 - 15 * u + 6 * u * u)
        return {k: self.start.get(k, v) + (v - self.start.get(k, v)) * s
                for k, v in self.end.items()}

    def moving(self, t: float) -> bool:
        """Whether this primary moves at ``t`` (aliveness waits for it)."""
        return t < self.t0 + self.duration


PEAK_SPEED = dict(VMAX)  # deg/s, z in m/s: a goto never outruns the slew
MIN_MOVE_S = 0.4
_MIN_JERK_PEAK = 1.875  # min-jerk peak speed = 1.875 * distance / duration


def goto(start: dict, end: dict, t0: float, speed: float = 1.0) -> Goto:
    """Return a Goto slow enough that no key's peak speed passes its cap, nor under MIN_MOVE_S.

    ``speed`` (SAGE_VOICE_MOTION_SPEED) divides the whole duration: the same move, time-scaled.
    Keys with no cap (the head's x and y) do not set it.
    """
    return Goto(start, end, t0, max(
        [MIN_MOVE_S] + [_MIN_JERK_PEAK * abs(v - start.get(k, v)) / PEAK_SPEED[k]
                        for k, v in end.items() if k in PEAK_SPEED]) / speed)


@dataclass(frozen=True)
class State:
    """The desired pose a primary left (held after it ends), and when primary motion last ran.

    ``sent`` is the last target, ``vel`` its per-key velocity, ``goal`` the clamped pose it
    chased and ``t`` when it was sent: the slew limit's memory (None before the first tick).
    """

    base: dict = field(default_factory=lambda: dict(NEUTRAL))
    moved_at: float = -math.inf
    sent: dict | None = None
    vel: dict = field(default_factory=dict)
    goal: dict = field(default_factory=dict)
    t: float | None = None


def rebased(state: State, pose: dict, t: float) -> State:
    """Return ``state`` at rest on ``pose``: another writer (the daemon's tracker) took it there.

    A fresh start for aliveness too: the pose has what breathing added.
    """
    return replace(state, base={**state.base, **pose}, moved_at=t, sent=dict(pose), vel={},
                   goal=dict(pose), t=t)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _slew(prev: float, v0: float, goal: float, last_goal: float, key: str, dt: float,
          speed: float = 1.0) -> float:
    """Return the next value from ``prev`` (moving at ``v0``) toward ``goal``, inside the caps.

    The step follows the goal's own step, plus the most of the gap that can still brake to a
    stop on it, capped by VMAX, then kept within ``a`` of the last step (AMAX). Braking from a
    step ``d`` takes steps d, d - a, ... while positive: ``n`` whole ``a``s then the rest spread
    over the n + 1 steps. A goal moving within the caps is tracked exactly.
    ``speed`` scales VMAX by itself and AMAX by its square: a ramp sped up ``speed`` times
    keeps its shape (as ``goto`` divides its duration), so smoothness holds at any speed.
    """
    e, a, top = goal - prev, AMAX[key] * speed * speed * dt * dt, VMAX[key] * speed * dt
    follow = _clip(goal - last_goal, -top, top)
    gap = e - follow
    n = math.floor(math.sqrt(0.25 + 2 * abs(gap) / a) - 0.5)
    brake = n * a + (abs(gap) - a * n * (n + 1) / 2) / (n + 1)
    step = _clip(follow + math.copysign(min(brake, abs(gap)), gap), -top, top)
    step = _clip(step, v0 * dt - a, v0 * dt + a)
    return goal if abs(step - e) < 1e-9 else prev + step  # land exactly, past rounding


SETTLE_POS = 1e-6  # per key, in its unit
SETTLE_DT = 0.02  # at rest: slower than one 50 Hz tick of AMAX, as the slew's last braking step
STALL_S = 0.1  # no tick for this long (a reconnect blocks 1-12 s): the body stopped on ``sent``


def settled(state: State) -> bool:
    """Whether the slewed target rests on its goal: ``sent`` on ``goal``, its speed one tick of
    braking at most (so a landing tick counts, not one more)."""
    return state.sent is None or all(
        abs(state.sent[k] - state.goal.get(k, state.sent[k])) <= SETTLE_POS
        and abs(state.vel.get(k, 0.0)) <= AMAX[k] * SETTLE_DT for k in state.sent if k in VMAX)


def stopping(state: State, speed: float = 1.0) -> dict:
    """Return where the slewed target can brake to from its speed, clamped.

    A stop holds there, so the body slows onto it without coming back: ``v|v| / 2a`` past
    ``sent``, per key, ``a`` scaled by the speed's square as ``tick`` scales it.
    """
    return clamp({k: v + state.vel.get(k, 0.0) * abs(state.vel.get(k, 0.0))
                  / (2 * AMAX[k] * speed * speed) if k in AMAX else v
                  for k, v in state.sent.items()})


def moving(state: State, primary) -> bool:
    """Whether the next tick is a moving one: the primary still moved at the last moving tick,
    or that tick was moving and the slewed target has not settled since.

    So the first tick at or past a move's end sends its end pose, however late it comes, and
    aliveness fades in only once the target settles: a step is done when the body arrives.
    """
    return (primary is not None and primary.moving(state.moved_at)) or (
        state.moved_at == state.t and not settled(state))


def still_s(state: State, t: float, primary) -> float:
    """Seconds since primary motion last ran at ``t``: 0 while it runs. Feeds ``alive.offsets``."""
    return 0.0 if moving(state, primary) else t - state.moved_at


def tick(state: State, t: float, primary, offsets, speed: float = 1.0) -> tuple[State, dict]:
    """Return the next state and the clamped target at ``t``.

    ``primary`` is a Hold, Sample, Goto or None (hold the last pose); its pose merges over the
    held one. ``offsets`` is an iterable of dicts added key by key. ``speed`` scales the slew.
    """
    base = {**state.base, **primary.at(t)} if primary is not None else state.base
    moved_at = t if moving(state, primary) else state.moved_at
    pose = dict(base)
    for offset in offsets:
        for key, value in offset.items():
            pose[key] = pose.get(key, 0.0) + value
    goal = clamp(pose)
    if state.sent is None:  # the first tick sends the goal as is
        return State(base, moved_at, goal, {}, goal, t), goal
    if t <= state.t:  # no time passed: no move
        return replace(state, base=base, moved_at=moved_at), {**goal, **state.sent}
    # after a stall it starts again from rest, one tick's step, whatever time passed
    dt, was = (SETTLE_DT, {}) if t - state.t > STALL_S else (t - state.t, state.vel)
    sent = {k: _slew(state.sent[k], was.get(k, 0.0), v, state.goal.get(k, v), k, dt, speed)
            if k in VMAX and k in state.sent else v for k, v in goal.items()}
    # a braking overshoot or rounding never leaves the limits; the head stays within reach of
    # the slewed body (ponytail: the yaw then moves at the body's pace, an abrupt cap change)
    sent = {k: _clip(v, *LIMITS[k]) if k in LIMITS else v for k, v in sent.items()}
    if "body_yaw" in sent and "yaw" in sent:
        low, high = (sent["body_yaw"] + off for off in LIMITS["head_yaw"])
        sent["yaw"] = _clip(_clip(sent["yaw"], low, high), *LIMITS["yaw"])
    vel = {k: (sent[k] - state.sent[k]) / dt for k in VMAX if k in sent and k in state.sent}
    return State(base, moved_at, sent, vel, goal, t), sent
