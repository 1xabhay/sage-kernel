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
from itertools import pairwise

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


#: Keys a recorded clip is fitted into: the ones with a range of their own (the head's yaw is
#: limited relative to the body, so it stays with ``clamp``).
FIT_KEYS = ("z", "pitch", "roll")


def fitted(clip: Callable[[float], dict], duration: float, dt: float = 0.02) -> Callable:
    """Return ``clip`` moved inside ``LIMITS`` once, so its shape survives the clamp.

    Pollen's emotes rest the head 20 mm up and bob about it; the per-tick clamp flattened every
    bob past the cap. A key whose range fits is shifted the least that brings it inside; one
    wider than the limits is scaled about its middle first. A clip already inside is returned.
    Sampling a clip costs about 60 ms (394 samples): call it off the event loop.
    """
    poses = [clip(min(i * dt, duration)) for i in range(int(duration / dt) + 1)]
    fit = {}
    for key in FIT_KEYS:
        values = [p[key] for p in poses if key in p]
        low, high = LIMITS[key]
        if not values or (low <= min(values) and max(values) <= high):
            continue
        lo, hi = min(values), max(values)
        scale = min(1.0, (high - low) / (hi - lo)) if hi > lo else 1.0
        mid, half = (lo + hi) / 2, (hi - lo) / 2 * scale
        fit[key] = (scale, mid, min(max(mid, low + half), high - half))
    if not fit:
        return clip

    def at(s: float) -> dict:
        pose = clip(s)
        return {**pose, **{k: centre + (pose[k] - mid) * scale
                           for k, (scale, mid, centre) in fit.items() if k in pose}}
    return at


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
    ``clamped``: the keys of that tick's pose past ``limits.py`` (eval A5 counts these).
    """

    base: dict = field(default_factory=lambda: dict(NEUTRAL))
    moved_at: float = -math.inf
    sent: dict | None = None
    vel: dict = field(default_factory=dict)
    goal: dict = field(default_factory=dict)
    t: float | None = None
    clamped: tuple = ()


def rebased(state: State, pose: dict, t: float) -> State:
    """Return ``state`` at rest on ``pose``: another writer (the daemon's tracker) took it there.

    A fresh start for aliveness too: the pose has what breathing added.
    """
    return replace(state, base={**state.base, **pose}, moved_at=t, sent=dict(pose), vel={},
                   goal=dict(pose), t=t)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _within(prev: float, value: float, low: float, high: float) -> float:
    """Return ``value`` inside the limits, or no further out than ``prev`` if that is past them."""
    return _clip(value, min(low, prev), max(high, prev))


def _slew(prev: float, v0: float, goal: float, last_goal: float, key: str, dt: float,
          speed: float = 1.0) -> float:
    """Return the next value from ``prev`` (moving at ``v0``) toward ``goal``, inside the caps.

    The step follows the goal's own step, plus the most of the gap that can still brake to a
    stop on it, capped by VMAX, then kept within ``a`` of the last step (AMAX). Braking from a
    step ``d`` takes steps d, d - a, ... while positive: ``n`` whole ``a``s then the rest spread
    over the n + 1 steps. A goal moving within the caps is tracked exactly.
    ``speed`` scales VMAX by itself and AMAX by its square: a ramp sped up ``speed`` times
    keeps its shape (as ``goto`` divides its duration), so smoothness holds at any speed.
    The brake is planned in whole 50 Hz ticks (``SETTLE_DT``), whatever this step's ``dt``: a
    plan over other steps keeps speed whole ticks cannot lose, and the body comes back.
    """
    e, a, top = goal - prev, AMAX[key] * speed * speed * dt * dt, VMAX[key] * speed * dt
    follow = _clip(goal - last_goal, -top, top)
    gap = e - follow
    a0 = AMAX[key] * speed * speed * SETTLE_DT * SETTLE_DT
    n = math.floor(math.sqrt(0.25 + 2 * abs(gap) / a0) - 0.5)
    brake = n * a0 + (abs(gap) - a0 * n * (n + 1) / 2) / (n + 1)
    if brake < abs(gap):  # not landing this tick: this step at the plan's speed
        brake *= dt / SETTLE_DT
    step = _clip(follow + math.copysign(min(brake, abs(gap)), gap), -top, top)
    step = _clip(step, v0 * dt - a, v0 * dt + a)
    return goal if abs(step - e) < 1e-9 else prev + step  # land exactly, past rounding


def _slewed(prev: float, v0: float, goal: float, last_goal: float, key: str, dt: float,
            speed: float = 1.0) -> tuple[float, float]:
    """Return ``_slew``'s next value and its end speed, a late tick run as the ticks it stands for.

    In steps of one 50 Hz tick, the goal's own move spread over them: one coarse step would brake
    as if every later tick were as long, keeping speed the on-time ticks after it cannot lose.
    """
    n = max(1, math.ceil(dt / SETTLE_DT - 1e-9))  # none longer than one tick
    if n == 1:  # on time: one step, as it always was
        nxt = _slew(prev, v0, goal, last_goal, key, dt, speed)
        return nxt, (nxt - prev) / dt
    at = [last_goal + (goal - last_goal) * i / n for i in range(n)] + [goal]  # exact at the end
    for before, after in pairwise(at):
        nxt = _slew(prev, v0, after, before, key, dt / n, speed)
        prev, v0 = nxt, (nxt - prev) * n / dt
    return prev, v0


SETTLE_POS = 1e-6  # per key, in its unit
SETTLE_DT = 0.02  # at rest: slower than one 50 Hz tick of AMAX, as the slew's last braking step
STALL_S = 0.1  # no tick for this long (a reconnect blocks 1-12 s): the body stopped on ``sent``


def sudden(was: State, now: State, speed: float = 1.0) -> tuple[str, float, float] | None:
    """Return the head or body axis furthest past its cap between two ticks, speed and cap; or None.

    Only what comes after the slew can do it: the head-to-body reach above all. 1% over is
    rounding. The very first target has nothing to compare with.
    """
    if was.sent is None or now.t is None or was.t is None or now.t <= was.t:
        return None
    speeds = {k: abs(now.sent[k] - was.sent[k]) / (now.t - was.t) for k in VMAX
              if k not in ("left", "right") and k in was.sent and k in now.sent}
    key = max(speeds, key=lambda k: speeds[k] / VMAX[k], default=None)
    return (key, speeds[key], VMAX[key] * speed) if (
        key and speeds[key] > 1.01 * VMAX[key] * speed) else None


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
    clamped = tuple(k for k, v in pose.items() if abs(goal[k] - v) > 1e-9)
    if state.sent is None:  # the first tick sends the goal as is
        return State(base, moved_at, goal, {}, goal, t, clamped), goal
    if t <= state.t:  # no time passed: no move
        return replace(state, base=base, moved_at=moved_at), {**goal, **state.sent}
    # after a stall it starts again from rest, one tick's step, whatever time passed
    dt, was = (SETTLE_DT, {}) if t - state.t > STALL_S else (t - state.t, state.vel)
    slewed = {k: _slewed(state.sent[k], was.get(k, 0.0), v, state.goal.get(k, v), k, dt, speed)
              for k, v in goal.items() if k in VMAX and k in state.sent}
    sent = {k: slewed[k][0] if k in slewed else v for k, v in goal.items()}
    # a braking overshoot or rounding never leaves the limits; the head stays within reach of
    # the slewed body (ponytail: the yaw then moves at the body's pace, an abrupt cap change).
    # A target already past them (the tracker left the head there: ``rebased``) comes back by
    # the slew: these clips only stop it going further, or they would snap it in one tick.
    was = state.sent
    sent = {k: _within(was.get(k, v), v, *LIMITS[k]) if k in LIMITS else v for k, v in sent.items()}
    if "body_yaw" in sent and "yaw" in sent:
        off_low, off_high = LIMITS["head_yaw"]
        low, high = sent["body_yaw"] + off_low, sent["body_yaw"] + off_high
        before, body = was.get("yaw", sent["yaw"]), was.get("body_yaw", sent["body_yaw"])
        if before < body + off_low:  # past the old reach: not just left behind by the body
            low = min(low, before)
        if before > body + off_high:
            high = max(high, before)
        sent["yaw"] = _within(before, _clip(sent["yaw"], low, high), *LIMITS["yaw"])
    # the end speed, the next tick's to brake from; where a clip moved it, the speed it made
    vel = {k: end if sent[k] == value else (sent[k] - state.sent[k]) / dt
           for k, (value, end) in slewed.items()}
    return State(base, moved_at, sent, vel, goal, t, clamped), sent
