"""The executor: the head's only owner. Plans, stop, tracking, dance and overlays all go through it.

- One activity slot. ``run(plan)`` performs its steps in order; a new plan
  cuts the running one, whose ``run`` returns ``interrupted`` with what it got
  to. The first failed step ends the plan.
- Open-ended steps (track_face, listen_song, dance) are ``done`` once they
  start and keep going until the next plan or stop, unless they have a timeout.
- Tracking is sticky (owner, 2026-10-23): once locked on (track_face, or a
  search that found a face) only ``stop_tracking`` ends it, and then the body
  goes home (the SDK's init pose, body yaw 0). A plan that moves the head
  pauses it (weight 0) and gives it back when the plan ends; a dance gives it
  back when the dance ends, or goes home if there was none.
- ``stop()`` wins over every other motion: it cancels the plan, the dance and
  every overlay, ends moves in flight (``_halt``: the SDK's cancel_move and
  the daemon's stop_move) and freezes head and body where they are, not at home. Tracking
  survives it. The SDK's goto_target blocks and cannot be cut short, so moves
  are cut into segments of ``segment_s`` (at most 0.8 s): the freeze lands
  within one segment.
- ``hold()``/``resume()``: while the person speaks the body holds still after
  the segment in flight (motors ruin the STT), then carries on, after at most
  ``HOLD_MAX_S`` (owner, 2026-10-25); the dance gets the same as its ``hold``
  event. Tracking is not held.
- ``overlay(perform)`` borrows the head for an emote: it waits for the segment
  in flight, pauses tracking if a face is locked, performs, then gives the
  tracker its weight back.
  During a dance the head is the dance's: an overlay raises ``HeadBusyError``.
- Aliveness (``idle``, ``cues``): with nothing else moving the body for
  ``IDLE_AFTER_S`` it moves like something alive (``breathe``: ``IdleMotion``'s
  smoothed random wander and occasional idle actions); at a voice's onset the
  head glances toward it (``attend``), if the daemon heard speech, then holds still
  while they speak; once their words are on the way to the kernel a thinking glance
  (``think``) until the reply's audio (``replying``), the turn's end, or ``THINK_S``, breathing
  under it. With the arbiter, a listening reflex under all of it: antennas up at a voice's first
  frame (``perk``), a slow nod at ``reflex.PAUSE_S`` of silence (``nod``), offsets the runner adds,
  decided in ``sage_device.reflex``; each logs its first target. Anything else takes over at
  once; none of it while tracking, and none after a stop until spoken to again or given a plan. The body sways
  too, the head turning with it, never against it. Every glance, thinking
  decision, idle start, stop (and why) and idle action is logged at INFO.
- The motion link: the SDK refuses commands ("Lost connection with the server.")
  whenever its liveness check saw no daemon message for a second, though the
  socket stays open (live 2026-10-23). A refused command is logged once as the
  link going down and once as it coming back; idle motion carries on from the
  clock. Down for ``MOTION_DEAD_S`` is reported to ``link_lost`` (the run ends
  loudly); ``on_link(up, stalls)`` shows it to ``make status``.
- Tracking and the slew limit: the daemon's tracker moves the head outside it, so what we can do
  is at the edges. Letting go (``_hand_back``): the head's real pose becomes the target, at rest,
  first. Coming up (``_ramp_up``): with the arbiter, the weight rises over ``TRACK_RAMP_S`` once a
  face is seen. In between its own speed is the daemon's: it eases 15% of the way each 20 ms
  (abstract.py, no setting), so a face jumping, or the recentre 2 s after losing one, is fast.
- Recorded moves by voice (``emote``, ``dance_move``): an emotion intent or a
  recorded dance (``expressions.py``) played by ``play_move(kind, name)`` as an
  overlay; stop cuts it.

Following a face past the head's reach needs nothing here: with the SDK's
automatic body yaw the IK drags the body once the head is 65° off it. Head
poses are in the world frame, so a turn carries the head with the body, and a
look passes ``body_yaw=None`` (goto_target's default of 0 re-centres the body).
ponytail: look angles are first guesses, to tune on the robot. Robot calls run
one at a time, in order, on one worker thread, off the event loop.
"""

import asyncio
import contextlib
import contextvars
import functools
import inspect
import json
import logging
import math
import os
import random
import threading
import time
import urllib.request
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from types import SimpleNamespace

import numpy as np
from reachy_mini.io.protocol import StopMoveCmd
from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS, INIT_HEAD_POSE
from reachy_mini.utils import create_head_pose
from sage_device import reflex

from reachy_sage_voice import expressions, limits
from reachy_sage_voice.behaviour.search import CallOut, Found, GiveUp, Search
from reachy_sage_voice.motion import alive, arbiter, runner
from reachy_sage_voice.motion.arbiter import Goto, Hold, Sample, moving
from reachy_sage_voice.perception.face import Face, face_percept
from reachy_sage_voice.plan import OUT_OF_RANGE, Plan, Step
from reachy_sage_voice.skills import MAX_TURN

logger = logging.getLogger(__name__)

#: Head poses for look, in degrees. ponytail: first guesses, to tune on the robot.
LOOKS = {"left": {"yaw": 30}, "right": {"yaw": -30}, "up": {"pitch": -20}, "down": {"pitch": 20},
         "center": {}}
ME_YAW = 40  # degrees: the furthest "look at me" turns the head toward the voice
#: Tracking weight while searching. Any weight above 0 runs the daemon's detector and updates
#: the face it reports (weight 0 pauses it and clears the face: backend enable_head_tracking,
#: step_head_tracking); the weight is only how far the head is pulled toward a face it sees.
SEARCH_WEIGHT = 0.05
#: The weight comes up from SEARCH_WEIGHT over this, once a face is seen. Tracking's first move
#: starts near 225 degrees/s on a 30 degree offset; the blend makes it the offset over this time.
#: The daemon latches a weight at its next detection (~10 fps), so it is a staircase of about 3
#: degrees a step. ponytail: 0 turns it off; a guess, to tune on the robot.
TRACK_RAMP_S = 1.0
RAMP_STEPS = 10
RAMP_POLL_S = 0.2  # how often a ramp waiting for a face reads it
#: Steps that pause tracking while they run.
HEAD_SKILLS = ("look", "turn", "dance", "stop", "emote", "dance_move")

# Aliveness (owner, 2026-10-23: "Reachy looks stuck", then "a real, alive pet, not a
# metronome"). ponytail: every number below is a first guess, to tune live on the robot;
# SAGE_VOICE_IDLE=0 turns idle motion and the glance off, SAGE_VOICE_LISTEN_CUES=0 the thinking cue.
# Idle motion starts after 0.3 s with nothing else moving, as Pollen's breathing does
# (reachy_mini_conversation_app, Apache-2.0, c178d4a, moves.py), but is organic: every channel
# is three slow sines with random periods and phases, plus an idle action now and then.
# Owner, 2026-10-23: head moves of 2.5-4 degrees and no body "look like a 2D poster", so the
# head wanders about 12 degrees of yaw, 6 of pitch and 4 of roll, and the body sways about 8.
# ponytail: the amplitudes are first guesses to tune live: SAGE_VOICE_IDLE_SCALE multiplies
# them all (0.5 calmer, 1.5 livelier), within IDLE_LIMITS.
IDLE_AFTER_S = 0.3  # quiet this long before idle motion starts
# ponytail: a guess; a move settles within ~1 s of its end at the real caps. Past this, the
# settle wait warns and carries on rather than hang a plan on a target that never rests.
SETTLE_TIMEOUT_S = 3.0
IDLE_TICK_S = 0.04  # set_target every 40 ms (25 Hz): the motion is slow, the link is busy
IDLE_EASE_S = 1.5  # from still to full motion
#: Channels: roll, pitch, yaw (degrees, the head's frame), height (metres), left and right
#: antenna (degrees, each its own tempo). Amplitude, the (shortest, longest) period. The yaw is
#: the turn of head and body together: the body takes IDLE_BODY_SHARE of it, the head the
#: rest on top, so they never counter-rotate (12 degrees of head on 8 of body).
IDLE_AMPLITUDE = (4.0, 6.0, 20.0, 0.010, 14.0, 14.0)
IDLE_PERIODS_S = ((5, 13), (4, 11), (8, 18), (3, 6), (1.5, 4.5), (1.5, 4.5))
IDLE_BODY_SHARE = 0.4
#: Limits of the offsets ``IdleMotion.at`` returns: those six (yaw: the head's off the body),
#: then the body's yaw.
IDLE_LIMITS = (15.0, 10.0, 35.0, 0.015, 40.0, 40.0, 15.0)
#: An idle action every so often (seconds, drawn uniformly): name -> seconds it lasts.
IDLE_ACTION_EVERY_S = (4.0, 10.0)
IDLE_ACTIONS = {"look_around": 4.0, "curious_tilt": 2.5, "antenna_twitch": 0.8}
# The glance toward a voice at its onset: head yaw = 90 - DoA degrees (community formula),
# relative to the body, within the head's reach of it.
GLANCE_MAX_YAW = 55.0  # degrees either side of the body
GLANCE_PITCH = -5.0  # degrees, a little up, toward a face
GLANCE_COOLDOWN_S = 1.75  # after any move, for a thought too: its motor noise is not the next voice
CUE_MOVE_S = 0.3  # a glance's or a thought's move
# Thinking: once a transcript goes to the kernel and before the reply's audio, a glance up and
# aside with the antennas tilted, breathing under it, back as soon as the reply starts, the turn
# ends with none, or after THINK_S. Replies take 3.4 s, p95 9.7 s. ponytail: to judge live.
THINK_S = 6.0
# Degrees, in the head's frame. At 14 the min-jerk curve wanted 422 deg/s^2 against AMAX's 240 at
# the 60 deg/s cap, a flick; this stays on its curve in MIN_MOVE_S, at 28 deg/s.
# ponytail: to tune live; 2.5-4 degrees read as a poster (owner, 2026-10-23).
THINK_PITCH, THINK_YAW = -6.0, 6.0
THINK_ANTENNA_DEG = 20.0  # both antennas the same way: a quizzical tilt
#: A motion link refusing every command this long is dead: the run ends (RobotLinkLostError).
MOTION_DEAD_S = 10.0
_COMMANDS = ("set_target", "goto_target", "start_head_tracking", "stop_head_tracking")


def idle_scale(env=os.environ) -> float:
    """Return SAGE_VOICE_IDLE_SCALE, the multiplier of every idle amplitude (default 1)."""
    said = env.get("SAGE_VOICE_IDLE_SCALE", "1")
    try:
        scale = float(said)
    except ValueError:
        scale = math.nan
    if not 0 <= scale <= 10:  # nan too
        logger.warning("SAGE_VOICE_IDLE_SCALE=%r is no multiplier from 0 to 10: using 1", said)
        return 1.0
    return scale


def motion_speed(env=os.environ) -> float:
    """Return SAGE_VOICE_MOTION_SPEED, the arbiter's move and slew speed (default 1)."""
    said = env.get("SAGE_VOICE_MOTION_SPEED", "1")
    try:
        speed = float(said)
    except ValueError:
        speed = math.nan
    if not 0.25 <= speed <= 2:  # nan too
        logger.warning("SAGE_VOICE_MOTION_SPEED=%r is no speed from 0.25 to 2: using 1", said)
        return 1.0
    return speed


class IdleMotion:
    """Organic idle motion as a function of time: smoothed random wander and idle actions.

    ``at(t)`` returns offsets for roll, pitch, yaw (the head's, off the body),
    height, left and right antenna (``IDLE_AMPLITUDE`` units) and the body's yaw,
    times ``scale``, within ``IDLE_LIMITS``; head and body yaw share a sign. Each channel sums
    three sines (weights 0.55, 0.3, 0.15) with periods and phases drawn once;
    an idle action (a slow look around, a curious tilt, one antenna's twitch)
    starts every ``IDLE_ACTION_EVERY_S``, eased in and out. ``history`` lists
    the actions as (start, name).
    """

    def __init__(self, rng: random.Random, scale: float = 1.0) -> None:
        """Draw this robot's own periods and phases from ``rng``."""
        self.rng, self.scale = rng, scale
        self.waves = [[(weight, 2 * math.pi / rng.uniform(*periods), rng.uniform(0, 2 * math.pi))
                       for weight in (0.55, 0.3, 0.15)] for periods in IDLE_PERIODS_S]
        self.history: list[tuple[float, str]] = []
        self._next: float | None = None
        self._action: tuple[float, str, tuple] | None = None

    def at(self, t: float) -> tuple[float, ...]:
        """Return the offsets at time ``t`` (seconds, any origin, increasing)."""
        wander = [amplitude * sum(w * math.sin(omega * t + phase) for w, omega, phase in waves)
                  for amplitude, waves in zip(IDLE_AMPLITUDE, self.waves, strict=True)]
        action = self._acting(t)
        roll, pitch, turn, z, left, right = (self.scale * (a + b)
                                             for a, b in zip(wander, action, strict=True))
        body = IDLE_BODY_SHARE * turn  # the same way as the head: clamping keeps the sign
        return tuple(max(-limit, min(limit, v)) for v, limit in zip(
            (roll, pitch, turn - body, z, left, right, body), IDLE_LIMITS, strict=True))

    def _acting(self, t: float) -> tuple[float, ...]:
        if self._next is None:
            self._next = t + self.rng.uniform(*IDLE_ACTION_EVERY_S)
        if t >= self._next:
            name = self.rng.choice(list(IDLE_ACTIONS))
            self._action = (self._next, name, self._peak(name))
            self.history.append((self._next, name))
            logger.info("idle action: %s for %.1f s", name, IDLE_ACTIONS[name])
            self._next += self.rng.uniform(*IDLE_ACTION_EVERY_S)
        if self._action is None:
            return (0.0,) * 6
        start, name, peak = self._action
        u = (t - start) / IDLE_ACTIONS[name]
        if not 0 <= u <= 1:
            return (0.0,) * 6
        return tuple(p * math.sin(math.pi * u) ** 2 for p in peak)  # out and back, smoothly

    def _peak(self, name: str) -> tuple[float, ...]:
        rng, side = self.rng, self.rng.choice((-1, 1))
        if name == "look_around":
            return (0.0, rng.uniform(-4, 2), side * rng.uniform(15, 30), 0.0, 0.0, 0.0)
        if name == "curious_tilt":
            lift = rng.uniform(5, 12)
            return (side * rng.uniform(8, 14), -rng.uniform(2, 5), 0.0, 0.004, lift, lift)
        twitch = side * rng.uniform(20, 30)  # antenna_twitch: one antenna
        return (0.0, 0.0, 0.0, 0.0, twitch, 0.0) if rng.random() < 0.5 else (
            0.0, 0.0, 0.0, 0.0, 0.0, twitch)


#: What the detector sees of the camera's ~87x67 degrees (edges distorted, YuNet at 320 px
#: wide), in degrees. ponytail: faces beyond ~3 m may be too small for YuNet at 320 px.
H_FOV, V_FOV = 70, 60
VIEW_STEP_DEG = 60  # a view at least this often along a move: the detector gets to look
MAX_BODY = MAX_TURN  # the body's own reach, degrees (limits.py)
FACE_READ_S = 0.5  # a face read's HTTP timeout: a CPU-starved daemon can take all of it
HOLD_MAX_S = 8.0  # the person speaking holds the body at most this long (owner, 2026-10-25)


@dataclass(frozen=True)
class SearchTiming:
    """How long the search waits at each view, in seconds (the daemon detects at ~10 fps)."""

    settle_s: float = 0.3  # after the move, before reading: the image stops blurring
    dwell_s: float = 0.6  # reading the face through this: ~6 frames
    poll_s: float = 0.1  # between reads
    confirm_s: float = 0.5  # a glimpse is seen again within this before locking on


SEARCH_TIMING = SearchTiming()


@dataclass(frozen=True)
class Result:
    """How a plan ended: done, failed (with reason) or interrupted, and a short text."""

    status: str
    reason: str | None = None  # not_found | timeout | unsupported | out_of_range | error
    text: str = ""


class HeadBusyError(RuntimeError):
    """The head is dancing: no emote now."""


class _FailedError(Exception):
    def __init__(self, reason: str, text: str):
        super().__init__(reason)
        self.reason, self.text = reason, text


def daemon_doa(url: str, timeout_s: float = 0.5,
               retry_s: float = 0.1) -> Callable[[], float | None]:
    """Read the robot daemon's ``GET /api/state/doa``: radians (0 left, π/2 front or back, π right).

    None when there is no reading. The daemon's poller starts on demand, so the
    first read after a minute idle is null: it is read once more ``retry_s`` later.
    """
    def once() -> float | None:
        read.speech_detected = None
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as response:
                body = json.loads(response.read())
        except (OSError, ValueError) as error:
            logger.warning("no direction of arrival: %s", error)
            return None
        if isinstance(body, dict):
            read.speech_detected = body.get("speech_detected")
        angle = body.get("angle") if isinstance(body, dict) else None
        return float(angle) if isinstance(angle, int | float) else None

    def read() -> float | None:
        angle = once()
        if angle is None:
            time.sleep(retry_s)  # called off the event loop (Executor._doa)
            angle = once()
        return angle
    return read


def daemon_face(url: str, timeout_s: float = FACE_READ_S) -> Callable[[], SimpleNamespace | None]:
    """Read the robot daemon's ``GET /api/media/tracking/face``: its face reading now.

    Fresh on every read; the SDK's ``get_tracked_face(wait=False)`` returns the
    daemon status, which is pushed once a second. The reading has ``detected``,
    ``x`` and ``ts`` (the daemon's clock), as the SDK's; None when it cannot be read.
    """
    def read() -> SimpleNamespace | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as response:
                body = json.loads(response.read())
        except (OSError, ValueError) as error:
            logger.warning("no face reading: %s", error)
            return None
        target = body.get("face_target", body) if isinstance(body, dict) else None  # 1.10 nests it
        if not isinstance(target, dict):
            return None
        return SimpleNamespace(detected=bool(target.get("detected")), x=target.get("x"),
                               ts=target.get("ts"))
    return read


def daemon_detector(url: str, timeout_s: float = FACE_READ_S) -> Callable[[float], bool | None]:
    """Ask the robot daemon's ``POST /api/media/tracking/enable`` to run its face detector.

    The SDK's websocket drops the daemon's reply, so a refusal (no camera) went
    unseen; this reads it. True if it runs, False (logged as a warning) if refused,
    None when it cannot be asked.
    """
    def enable(weight: float) -> bool | None:
        request = urllib.request.Request(url, data=json.dumps({"weight": weight}).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                body = json.loads(response.read())
        except (OSError, ValueError) as error:
            logger.warning("could not ask the face detector: %s", error)
            return None
        enabled = body.get("enabled") if isinstance(body, dict) else None
        if enabled is False:
            logger.warning("the face detector refused (%s): no camera? presence stays unknown",
                           body.get("status"))
        return enabled
    return enable


def head_yaw(head) -> float:
    """Return the world yaw of a 4x4 head pose, in degrees, left positive."""
    return math.degrees(math.atan2(head[1][0], head[0][0]))


def within_limits(target: dict) -> dict:
    """Return goto_target/set_target arguments with head and body inside ``limits.py``.

    The daemon drops a target past a limit, which reads as a freeze (A5). The head is
    rebuilt, keeping its position, only when its angles were cut.
    """
    head, body = target.get("head"), target.get("body_yaw")
    pose = {} if body is None else {"body_yaw": math.degrees(body)}
    if head is not None:
        m = np.asarray(head, dtype=float)
        pose |= {"roll": math.degrees(math.atan2(m[2, 1], m[2, 2])),
                 "pitch": math.degrees(math.asin(max(-1.0, min(1.0, -m[2, 0])))),
                 "yaw": math.degrees(math.atan2(m[1, 0], m[0, 0]))}
    held = limits.clamp(pose)
    if held == pose:
        return target
    out = dict(target)
    if body is not None:
        out["body_yaw"] = math.radians(held["body_yaw"])
    if head is not None and any(held[k] != pose[k] for k in ("roll", "pitch", "yaw")):
        out["head"] = create_head_pose(*m[:3, 3], held["roll"], held["pitch"], held["yaw"])
    return out


class Executor:
    """One activity slot on one robot; see the module notes."""

    def __init__(self, mini, doa: Callable[[], float | None] = lambda: None,
                 listen: Callable[[], Awaitable] | None = None, dance: Callable | None = None,
                 audio: Callable[[float], bytes] = lambda seconds: b"", segment_s: float = 0.6,
                 step_deg: float = 40, face: Callable[[], bool | None] | None = None,
                 timing: SearchTiming = SEARCH_TIMING, tracking: bool = False,
                 clock: Callable[[], float] = time.monotonic, idle: bool = False,
                 cues: bool = False, play_move: Callable[[str, str], Awaitable] | None = None
                 ) -> None:
        """``mini`` is a connected ReachyMini.

        ``doa`` returns the voice's direction in radians, 0 left, π/2 front, π right
        (``daemon_doa``), or None. ``listen`` listens to a song (None: listen_song is
        unsupported). ``dance(mini, audio, stop_event, hold=event)`` is a coroutine
        that dances until the music ends or ``stop_event`` (a threading.Event) is
        set, holding still while ``hold`` (an asyncio.Event) is set; it is also
        cancelled on stop (None: dance is unsupported). ``audio(seconds)`` is
        the mic's last seconds. ``step_deg`` is the body yaw per segment of a turn.
        ``face()`` returns the face percept (``perception.face.Face``); None, or it
        returning None, judges the SDK's reading here, on ``time.monotonic``, the
        detector on while tracking at a weight above 0; ``timing`` paces the
        search. ``tracking``: the tracker is already on at weight 1
        (SAGE_VOICE_PRESENCE), so it is locked on. ``idle``: breathe (``breathe``)
        and glance toward a voice (``attend``); ``cues``: the thinking cue (``think``).
        ``play_move(kind, name)`` plays a recorded move, kind ``emotion`` or ``dance``
        (None: emote and dance_move are unsupported).
        """
        assert 0 < segment_s <= 0.8, "stop must land within a second"
        self.mini, self.doa, self.listen, self.dance, self.audio = mini, doa, listen, dance, audio
        self.segment_s, self.step = segment_s, math.radians(step_deg)
        self.face, self.timing, self.clock = face, timing, clock
        self._robot = ThreadPoolExecutor(max_workers=1, thread_name_prefix="executor")
        self._head = asyncio.Lock()  # a segment or an overlay holds the head
        self._plan: asyncio.Task | None = None
        self._activity: asyncio.Task | None = None  # an open-ended step's own task
        self._dancing: threading.Event | None = None  # set to end the dance
        self._tracking: float | None = 1.0 if tracking else None  # the weight sent, while on
        self._locked = tracking  # sticky tracking: only stop_tracking ends it
        self._overlays: set[asyncio.Task] = set()
        self._held = asyncio.Event()  # set while the person speaks: the dance's ``hold``
        self._free = asyncio.Event()  # the opposite, for segments to wait on
        self._free.set()
        self._held_at, self._held_s = 0.0, 0.0
        self.idle, self.cues = idle, cues
        self._cue: asyncio.Task | None = None  # a glance or a thought
        self._away = None  # the head pose a thought glanced away from
        self._moved_at = -math.inf  # when the head last moved, for the glance's cooldown
        self._still = False  # stopped: no breathing or cue until spoken to or given a plan
        self._cue_kind = ""
        self._pondering = False  # a thought has landed and is held: breathing runs under it
        self._perk_at = self._quiet_at = self._nod_at = None  # the listening reflex's clock
        self._fired: dict[str, float] = {}  # when each reflex last fired (sage_device.reflex)
        self._commanded: tuple[str, float] | None = None  # one just fired, its target not yet sent
        self.play_move = play_move
        #: With the arbiter, ``clip(kind, name, sound=True)`` returns the recorded move (the
        #: SDK's Move, its sound started unless muted) for the runner to sample, in place of
        #: ``play_move``.
        self.clip: Callable[..., Awaitable] | None = None
        self.motion = IdleMotion(random.Random(), idle_scale())
        self.arbiter = os.environ.get("SAGE_VOICE_ARBITER") == "1"  # LAYERS.md §5 L5, one deploy
        self.speed = motion_speed()  # with the arbiter: moves and slew (SAGE_VOICE_MOTION_SPEED)
        self._runner: asyncio.Task | None = None  # the one motion runner (arbiter), once started
        self._primary: Goto | Sample | Hold | None = None  # what it plays
        self._dropped = False  # the tracking weight is 0 while a Sample plays (LAYERS §1a row 5)
        self._ramp, self._ramping = 1.0, None  # the most of the weight sent, and what raises it
        self._step = None  # the runner's step, to rebase when tracking lets go of the head
        self._ticked = -math.inf  # the clock of its last tick
        self._sent: dict | None = None  # the target of that tick
        self._state = arbiter.State()  # the arbiter's state after it
        self._brake: Hold | None = None  # where it brakes to before yielding (``_yields``)
        self._idling: Hold | None = None  # the Hold idle took the body with: not a move's
        self._dwell: Hold | None = None  # a search view's Hold, breathing under it
        self._failed: Exception | None = None  # why it last failed: a Goto waiting fails with it
        self.link_lost: Callable[[str], None] = lambda reason: logger.error("%s", reason)
        self.on_link: Callable[[bool, int], None] = lambda up, stalls: None
        self._down_since: float | None = None  # the motion link refusing commands, since
        self._stalls, self._dead = 0, False
        self.search, self.hold_max_s = Search(), HOLD_MAX_S
        #: Say "<owner>, where are you?" (sage.py); None: the search calls out silently.
        self.call_out: Callable[[], Awaitable] | None = None
        self._heard = False  # the person spoke since the search last asked
        self._last_seen: float | None = None  # world yaw of the face a search last found

    # --- holding still while the person speaks --------------------------------

    def hold(self) -> None:
        """Hold still after the segment in flight: the person started speaking (owner, 2026-10-23).

        Motors moving ruin the speech-to-text (reachy_mini #1334). Tracking is not
        held; stop and a new plan still act at once. Breathing stops too.
        """
        self._still = False  # spoken to: alive again after a stop
        self._heard = True
        if not self._held.is_set():
            self._held_at = self.clock()
        self._held.set()
        self._free.clear()

    def resume(self) -> None:
        """Carry on from where the body was held: the person stopped speaking."""
        if self._held.is_set():
            self._held_s += self.clock() - self._held_at
        self._held.clear()
        self._free.set()

    def _moving_s(self) -> float:
        """Return the clock less the time held: a search's limit counts only moving time."""
        return self.clock() - self._held_s

    # --- the activity slot ----------------------------------------------------

    async def run(self, plan: Plan) -> Result:
        """Perform ``plan``, cutting whatever ran before; return how it ended."""
        self.resume()  # a plan is the person's (or the model's) answer: they have finished
        self._still = False
        self._end_cue("a plan")
        await self._end_activity()
        if self._tracking and any(step.skill in HEAD_SKILLS for step in plan):
            await self._weight(0.0)
        done: list[str] = []
        task = self._plan = asyncio.ensure_future(self._perform(plan, done))
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task.cancelled():
            return Result("interrupted", text="; ".join(done))
        if self._plan is task:
            self._plan = None
            if self._locked and self._dancing is None:
                await self._weight(1.0)  # the plan is over: the tracker has the head again
        return task.result()

    async def stop(self) -> None:
        """Stop every motion but tracking and freeze where head and body are, within a second."""
        plan, self._plan = self._plan, None
        brake = self._primary = self._braking()  # the runner brakes onto it from its next tick
        if plan:
            plan.cancel()
        self._still = True
        self._end_cue("stop")
        self._end_dance()
        for task in (*self._overlays, self._activity):
            if task:
                task.cancel()
        self._activity = None
        self._halt()
        if not self._locked:
            await self._untrack()  # a search's own tracking
        await self._freeze(brake)
        if self._locked:
            await self._weight(1.0)

    async def overlay(self, perform: Callable[[], Awaitable | None]):
        """Borrow the head for ``perform()`` (an emote), then hand it back."""
        if self._dancing is not None:
            raise HeadBusyError("the head is dancing")
        task = asyncio.current_task()
        self._overlays.add(task)
        try:
            async with self._head:
                # Pause only once a face is locked, else the emote blocks acquiring one: from
                # Pollen's reachy_mini_conversation_app (Apache-2.0, c178d4a, moves.py:376-392).
                paused = (self._tracking if not self.arbiter and self._tracking
                          and await self._face() else None)  # with the arbiter: _play's
                if paused:
                    await self._hand_back()
                    await self._reflex("start_head_tracking", weight=0.0)
                try:
                    result = perform()
                    return await result if inspect.isawaitable(result) else result
                finally:
                    if paused and self._tracking == paused:  # not if a plan changed it meanwhile
                        await self._reflex("start_head_tracking", weight=paused)
        finally:
            self._overlays.discard(task)
            self._moved_at = self.clock()

    # --- aliveness: breathing, a glance toward the voice, a thinking cue --------

    async def breathe(self) -> None:
        """Move like something alive, forever, whenever nothing else has moved the body for a while.

        Anything else (a plan, an emote, a cue, tracking, the person speaking, a
        stop) takes over at the next tick; idle motion eases back in after. Run it
        as a task beside the voice; it returns at once unless ``idle``. With the arbiter it is
        the motion runner's life instead: cancelling it stops the runner.
        """
        if self.arbiter:
            return await self._run_motion()
        while self.idle:
            try:
                await self._until_idle()
                await self._idle()
            except Exception as error:  # a reflex, not a requirement
                logger.warning("idle motion failed (%s) - trying again", error)
                await asyncio.sleep(1.0)

    def attend(self) -> None:
        """Someone started speaking: glance toward their voice (``idle``), then hold still."""
        self._start_cue(self._glance(), "glance", "new speech")

    def think(self) -> None:
        """Glance away, thinking: words went to the kernel. Never waits: the reply is on its way."""
        self._start_cue(self._think(), "think", "the turn ended again")

    def replying(self) -> None:
        """Come back from a thinking glance at once: the reply's audio started."""
        self._start_cue(self._come_back(), "come back", "the reply started")

    def busy(self) -> str | None:
        """Why a gesture must not start now: the body is someone else's, or held still; or None."""
        return self._busy()

    def thought_over(self) -> None:
        """End the thought: the turn ended, maybe with no audio to; a glance is left alone."""
        if self._cue_kind == "think" and self._cue and not self._cue.done():
            self._start_cue(self._come_back(), "come back", "the turn ended")

    # --- the listening reflex: antennas at a voice, a nod at a pause (arbiter only) -------------

    def reflex_busy(self) -> str | None:
        """Why no reflex may show now, or None: a plan, a search, a dance, an emote, a stop."""
        return self._why_not(tracking=False) or ("dancing" if self._dancing is not None else None)

    def _reflex_fires(self, reaction: str, speaking: bool) -> bool:
        now = self.clock()
        if not (self.arbiter and self.cues and reflex.fires(
                reaction, now, self._fired.get(reaction, -math.inf), speaking=speaking,
                busy=self.reflex_busy())):
            return False
        self._fired[reaction], self._commanded = now, (reaction, now)
        self._run_motion()
        return True

    def perk(self, speaking: bool = False) -> None:
        """Lift the antennas at a voice's first frame, or keep them up if not yet down."""
        if self._reflex_fires(reflex.LISTENING, speaking):
            up = self._perk_at is not None and self._quiet_at is not None and (
                self.clock() - self._quiet_at < alive.PERK_FALL_S)
            self._perk_at = self._perk_at if up else self.clock()
            self._quiet_at = None

    def nod(self, speaking: bool = False) -> None:
        """Nod, small and slow, at a pause: right whether they go on or have finished."""
        if self._reflex_fires(reflex.PAUSE, speaking):
            self._nod_at = self.clock()

    def unperk(self) -> None:
        """Ease the antennas down: the voice is gone, whatever it turned out to be."""
        if self._perk_at is not None and self._quiet_at is None:
            self._quiet_at = self.clock()

    def _listening(self, t: float) -> tuple:
        return tuple(None if at is None else t - at
                     for at in (self._perk_at, self._quiet_at, self._nod_at))

    def _reflex_pose(self, t: float) -> dict:
        return alive.listening(*self._listening(t))

    def _busy(self) -> str | None:
        """Why something other than idle motion has the body, or wants it still; or None.

        With the arbiter tracking is not: the runner breathes under it, the daemon steering the
        head (at weight 1 it drops set_target's head, never the antennas or body yaw).
        """
        if why := self._why_not(tracking=not self.arbiter):
            return why
        if self._held.is_set():
            return "the person is speaking"
        if self._head.locked():
            return "a move has the head"
        if self._cue and not self._cue.done() and not self._pondering:
            return f"a {self._cue_kind} cue"
        return None

    def _why_not(self, tracking: bool = True) -> str | None:
        """Why no cue may move the head now, or None; ``tracking`` False: tracking is no reason."""
        running = (("a plan is running", self._plan), ("an activity is running", self._activity))
        for why, busy in (("stopped", self._still),
                          ("tracking", tracking and self._tracking is not None),
                          ("dancing", self._dancing is not None and not self.arbiter),
                          ("an emote is playing", self._overlays),
                          *((why, task and not task.done()) for why, task in running)):
            if busy:
                return why
        return None

    def _may_cue(self) -> bool:
        return self._why_not() is None

    async def _until_idle(self) -> None:
        since = None
        while since is None or self.clock() - since < IDLE_AFTER_S:
            await asyncio.sleep(IDLE_TICK_S)
            since = None if self._busy() else self.clock() if since is None else since

    async def _idle(self) -> None:
        """Move around where head and antennas are, until something else wants the body.

        A refused command (the motion link stalled) skips its tick: the motion is a
        function of the clock, so it carries on where it would have been.
        """
        head = np.array(await self._call("get_current_head_pose"), dtype=float)
        joints, antennas = await self._call("get_current_joint_positions")
        room = max(0.0, GLANCE_MAX_YAW - abs(head_yaw(head) - math.degrees(joints[0])))
        start = self.clock()
        logger.info("idle motion started (scale %g)", self.motion.scale)
        while not (why := self._busy()):
            ease = min(1.0, (self.clock() - start) / IDLE_EASE_S)
            roll, pitch, yaw, z, left, right, body = (
                ease * v for v in self.motion.at(self.clock()))
            # World frame: the head turns with the body, and its own yaw on top, the same way.
            target = head @ create_head_pose(roll=roll, pitch=pitch,
                                             yaw=body + max(-room, min(room, yaw)))
            target[2, 3] += z
            with contextlib.suppress(ConnectionError):  # _call logged the link going down
                await self._call("set_target", head=target, antennas=[
                    antennas[0] + math.radians(left), antennas[1] + math.radians(right)],
                    body_yaw=joints[0] + math.radians(body))
            await asyncio.sleep(IDLE_TICK_S)
        logger.info("idle motion stopped: %s", why)

    # --- the motion runner (SAGE_VOICE_ARBITER=1, LAYERS.md §5 L5 step 2) ------------

    def _run_motion(self) -> asyncio.Task:
        """Start the one motion runner, once; return its task."""
        if self._runner is None or self._runner.done():
            self._runner = asyncio.ensure_future(self._drive())
        return self._runner

    async def _drive(self) -> None:
        """Play the primary at 50 Hz, breathing under it, while the body is the runner's.

        A Goto, a Sample or a move's Hold (a search view, a freeze) is always the runner's.
        Otherwise, with ``idle``, the body is its once nothing else has wanted it for
        ``IDLE_AFTER_S``: it holds where the body is then. It yields, sending nothing, whenever
        anything else wants the body (a plan, a cue, a search); not to tracking, which steers
        the head while it breathes (``_busy``).
        """
        def tick(t: float) -> dict:
            self._ticked, self._sent = t, step(t)
            self._state = step.state
            if self._still and isinstance(self._primary, Hold) and arbiter.settled(self._state):
                self._primary = None  # stopped: once the freeze is reached nothing moves
            return self._sent
        step = self._step = runner.idle(lambda: self._brake or self._primary, self.clock(),
                                        self.motion.scale, self._held.is_set, self.speed,
                                        self._reflex_pose)
        while True:
            try:
                await self._until_owned()
                logger.info("motion runner started (scale %g)", self.motion.scale)
                why = await runner.run(tick, self._send, self.clock, asyncio.sleep, self._yields,
                                       lambda: self._down_since is not None)
                logger.info("motion runner yielded: %s", why)
            except Exception as error:  # a reflex, not a requirement
                self._failed = error
                logger.warning("motion runner failed (%s) - trying again", error)
                await asyncio.sleep(1.0)

    async def _send(self, target: dict) -> None:
        sending = runner.SENDING.set(True)  # the one writer (M1)
        commanded, self._commanded = self._commanded, None
        began = self.clock()
        try:
            await self._call("set_target", **runner.command(target))
            if commanded:  # against a camera on the antennas this measures the motors
                logger.info("reflex %s: first target %d ms after its cue, sent in %d ms",
                            commanded[0], (began - commanded[1]) * 1000,
                            (self.clock() - began) * 1000)
        except ConnectionError as error:  # _call logged the link going down
            self._failed = error  # a move waiting fails with it, as a refused goto does
        finally:
            runner.SENDING.reset(sending)

    def _moving(self) -> bool:
        """Whether the primary is a move's: the runner's whatever else wants the body.

        Not idle's Hold, nor a search view's while the person speaks (hold: breathing stops).
        """
        p = self._primary
        return alive.listening_moves(*self._listening(self.clock())) or (
            p is not None and p is not self._idling
            and not (p is self._dwell and self._held.is_set()))

    def _yields(self) -> str | None:
        """Why the body is not the runner's now, or None.

        It never yields at speed, which would freeze the target there: it brakes first, onto
        where it can stop (also when a cancelled move leaves no primary), no offsets under it.
        """
        if self._moving():
            self._brake = None
            return None
        why = self._busy() if self.idle else "idle off"
        if arbiter.settled(self._state):
            self._brake = None
            return why
        if why or self._primary is None:
            self._brake = self._brake or Hold(arbiter.stopping(self._state, self.speed))
        return None

    async def _until_owned(self) -> None:
        """Wait for a move, or (``idle``) ``IDLE_AFTER_S`` of quiet, then hold the body there."""
        since = None
        while not self._moving():
            if since is not None and self.clock() - since >= IDLE_AFTER_S:
                self._primary = self._idling = Hold(runner.pose(*await self._pose()))
                return
            await asyncio.sleep(IDLE_TICK_S)
            quiet = self.idle and not self._busy()
            since = None if not quiet else self.clock() if since is None else since
        if self._primary is None or self._primary is self._idling:  # a reflex woke it: from here
            self._primary = self._idling = Hold(runner.pose(*await self._pose()))

    async def _goto(self, held: bool = True, **target) -> dict:
        """Move as ``_segment`` does; with the arbiter, as a Goto the runner plays.

        It returns once the runner has ticked the Goto's end, so a plan step still waits for
        its move. ``held`` False: not held while the person speaks (a cue: it is the onset).
        With the arbiter it returns the pose it went to, over as long as the distance needs.
        """
        if not self.arbiter:
            return await self._segment(**target)
        if held:
            await self._until_free()
        async with self._head:
            head, body, antennas = here = await self._pose()
            end = (target.get("head", head),
                   body if target.get("body_yaw") is None else target["body_yaw"],
                   antennas if target.get("antennas") is None else target["antennas"])
            move = arbiter.goto(runner.pose(*here), runner.pose(*end), self.clock(), self.speed)
            await self._play(move)
            return move.end

    async def _play(self, move: Goto | Sample | Hold) -> None:
        """Have the runner play ``move``; return once it has ticked its end and the slewed target
        has settled there (a Hold: once settled), or once something else replaced it (a stop).

        A Sample with a face locked on drops the tracking weight to 0 while it plays (at 1 the
        daemon would drop its head), then restores it. Only once a face is locked, else the
        emote blocks acquiring one: from Pollen's reachy_mini_conversation_app (Apache-2.0,
        c178d4a, moves.py:376-392).
        """
        start = self.clock()
        drop = isinstance(move, Sample) and bool(self._tracking) and await self._face()
        try:
            if drop:
                await self._hand_back()
                self._dropped = True
                self._end_ramp()
                await self._reflex("start_head_tracking", weight=0.0)
            self._primary, self._failed = move, None
            self._run_motion()
            deadline = start + getattr(move, "duration", 0.0) + SETTLE_TIMEOUT_S
            while self._ticked < start or (self._primary is move
                                           and moving(self._state, move)):
                if self.clock() > deadline:
                    logger.warning("motion did not settle in %g s - carrying on",
                                   deadline - start)
                    break
                if self._failed:  # a refused or timed-out target: the move did not happen
                    raise self._failed
                await asyncio.sleep(runner.DT / 2)
        finally:
            if self._primary is move:
                self._primary = None
            if drop:
                self._dropped = False
                self._start_ramp()
                await self._reflex_weight()
            self._moved_at = self.clock()

    async def _sample(self, kind: str, name: str, sound: bool = True) -> None:
        """Play a recorded move as a Sample: the runner evaluates the clip every tick."""
        move = await self.clip(kind, name, sound)

        def at(s: float) -> dict:  # the SDK's evaluate raises at the clip's end, as its player
            head, antennas, body_yaw = move.evaluate(max(0.0, min(s, move.duration - 1e-2)))
            return runner.pose(head, body_yaw, antennas)
        await self._play(Sample(at, self.clock(), move.duration))

    async def _pose(self) -> tuple:
        """Return the head pose, body yaw and antennas where the body is now, under the reflex.

        The reflex is an offset the runner adds: a move ending on what it read would keep it.
        ponytail: a stop's Hold is the sent target, reflex and all, until the next move.
        """
        head = np.array(await self._call("get_current_head_pose"), dtype=float)
        joints, antennas = await self._call("get_current_joint_positions")
        if off := self._reflex_pose(self.clock()):
            head = head @ create_head_pose(pitch=-off.get("pitch", 0.0))
            antennas = [antennas[0] - math.radians(off.get("left", 0.0)),
                        antennas[1] - math.radians(off.get("right", 0.0))]
        return head, joints[0], antennas

    def _start_cue(self, cue: Awaitable, kind: str, cause: str) -> None:
        self._cancel_cue(cause)
        self._cue, self._cue_kind = asyncio.ensure_future(cue), kind

    def _end_cue(self, cause: str) -> None:
        """Drop the cue, and any thought to come back from: a plan or stop has the head."""
        self._cancel_cue(cause)
        self._cue = self._away = None

    def _cancel_cue(self, cause: str) -> None:
        self._pondering = False
        if self._cue and not self._cue.done():
            if self._cue_kind == "think":
                logger.info("thinking cue cancelled: %s", cause)
            self._cue.cancel()

    async def _glance(self) -> None:
        """Turn the head toward the voice: at the onset only, never twice within the cooldown."""
        why = self._glance_why_not()
        angle = None
        if why is None:
            angle = await self._doa()
            # Not True: the angle is the chip's last one, not this voice's (or unknown: stay put).
            heard = getattr(self.doa, "speech_detected", None)
            why = (f"no direction of arrival (speech_detected={heard})"
                   if angle is None or heard is not True else self._glance_why_not())
        if why is not None:
            logger.info("glance skipped: %s", why)
            await self._come_back()
            return
        toward = max(-GLANCE_MAX_YAW, min(GLANCE_MAX_YAW, 90 - math.degrees(angle)))
        logger.info("glance: DoA %.0f deg (speech_detected=%s) -> head yaw %+.0f deg from the body",
                    math.degrees(angle), heard, toward)
        body = math.degrees(await self._yaw())
        self._away = None  # facing the voice beats coming back from a thought
        await self._cue_move(create_head_pose(yaw=body + toward, pitch=GLANCE_PITCH))

    def _glance_why_not(self) -> str | None:
        if not self.idle:
            return "idle off"
        if why := self._why_not():
            return why
        if self._down_since is not None:
            return "motion link down"
        return self._cooling()

    def _cooling(self) -> str | None:
        if (since := self.clock() - self._moved_at) < GLANCE_COOLDOWN_S:
            return f"cooldown ({since:.1f} s after a move)"
        return None

    async def _think(self) -> None:
        if not self.cues:
            return
        speaking = "the person is speaking" if self._held.is_set() else None
        if why := self._why_not() or speaking or self._cooling():
            logger.info("thinking cue skipped: %s", why)
            return
        here, _, antennas = await self._pose()
        self._away = (here, list(antennas))
        logger.info("thinking cue fired")
        tilt = math.radians(THINK_ANTENNA_DEG)
        await self._cue_move(here @ create_head_pose(pitch=THINK_PITCH, yaw=THINK_YAW),
                             [antennas[0] + tilt, antennas[1] + tilt])
        self._pondering = True  # the move is over: breathing may run under the held pose
        await asyncio.sleep(THINK_S)
        self._pondering = False
        await self._come_back()

    async def _come_back(self) -> None:
        away, self._away = self._away, None
        if away is not None and self._may_cue():
            await self._cue_move(*away)

    async def _cue_move(self, head, antennas=None) -> None:
        """Move the head only, now: a cue is not held while the person speaks (it is the onset)."""
        if self.arbiter:
            return await self._goto(False, head=head, antennas=antennas, body_yaw=None)
        async with self._head:
            try:
                await self._call("goto_target", head=head, antennas=antennas,
                                 duration=CUE_MOVE_S, body_yaw=None)
            finally:
                self._moved_at = self.clock()

    async def _end_activity(self) -> None:
        old, self._plan = self._plan, None
        if old and not old.done():
            old.cancel()
            await asyncio.wait({old})
        self._end_dance()
        activity, self._activity = self._activity, None
        if activity:
            activity.cancel()
        if not self._locked:
            await self._untrack()  # a search's or a timed track's own tracking

    def _end_dance(self) -> None:
        dancing, self._dancing = self._dancing, None
        if dancing:
            dancing.set()  # a dance in a thread sees this at once; its task is cancelled too

    async def _perform(self, plan: Plan, done: list[str]) -> Result:
        try:
            for step in plan:
                text = await getattr(self, "_" + step.skill)(step)
                done.append(f"{text} ({OUT_OF_RANGE})" if step.note else text)
        except _FailedError as failure:
            return Result("failed", failure.reason, "; ".join([*done, failure.text]))
        except TimeoutError:
            return Result("failed", "timeout", "; ".join([*done, f"{step.skill} timed out"]))
        except Exception:
            logger.exception("%s failed", step.skill)
            return Result("failed", "error", "; ".join([*done, f"{step.skill} failed"]))
        return Result("done", text="; ".join(done))

    # --- skills ---------------------------------------------------------------

    async def _look(self, step: Step) -> str:
        pose = LOOKS.get(step.arg, {})
        if step.arg == "me":
            angle = await self._doa()
            yaw = 0.0 if angle is None else math.degrees(math.pi / 2 - angle)
            pose = {"yaw": max(-ME_YAW, min(ME_YAW, yaw))}
        await self._goto(head=create_head_pose(**pose), body_yaw=None)
        return f"looked {step.arg}"

    async def _turn(self, step: Step) -> str:
        here = await self._yaw()
        limit = math.radians(MAX_TURN)
        target = max(-limit, min(limit, here + math.radians(step.arg)))
        if math.isclose(target, here, abs_tol=1e-3):
            raise _FailedError(OUT_OF_RANGE, "already turned as far as the body goes")
        await self._sweep_to(here, target)
        turned = round(math.degrees(target - here))
        note = f" ({OUT_OF_RANGE})" if turned != step.arg and not step.note else ""
        return f"turned {abs(turned)} degrees {'left' if turned > 0 else 'right'}{note}"

    async def _track_face(self, step: Step) -> str:
        if step.until == "timeout":
            await self._weight(1.0)
            await asyncio.sleep(step.timeout_s)
            if not self._locked:
                await self._untrack()
            return f"tracked for {step.timeout_s:g} s"
        await self._lock()
        return "now tracking you"

    async def _stop_tracking(self, step: Step) -> str:
        self._locked = False
        await self._untrack()
        await self._home()
        return "stopped tracking"

    async def _listen_song(self, step: Step) -> str:
        if self.listen is None:
            raise _FailedError("unsupported", "this body cannot listen to songs")
        self._activity = asyncio.ensure_future(self.listen())
        if step.until == "timeout":
            await asyncio.wait({self._activity}, timeout=step.timeout_s)
            return f"listened for {step.timeout_s:g} s"
        return "now listening"

    async def _dance(self, step: Step) -> str:
        if self.dance is None:
            raise _FailedError("unsupported", "this body cannot dance")
        stop = self._dancing = threading.Event()
        self._activity = asyncio.ensure_future(self._dance_until(stop, step.timeout_s))
        return "now dancing"

    async def _dance_until(self, stop: threading.Event, timeout_s: float) -> None:
        try:
            await asyncio.wait_for(self._groove() if self.arbiter else
                                   self.dance(self.mini, self.audio, stop, hold=self._held),
                                   timeout_s)
        except TimeoutError:
            pass
        except Exception:
            logger.exception("the dance failed")
        finally:
            stop.set()
        # Over by itself (the music ended, or the timeout), not cut by stop or a plan.
        if self._activity is asyncio.current_task():
            self._activity = self._dancing = None
            if self._locked:
                await self._weight(1.0)
            else:
                await self._home()

    async def _groove(self) -> None:
        """With the arbiter: home, find the beat, then dance as one Sample on the beat clock.

        The Sample's clip runs the dance's phase-locked loop; the person speaking stops that
        clock (at most ``hold_max_s``). A stop clears the Sample and cancels this; ``_stop``
        ends nothing else (``_dancing`` only marks the dance).
        """
        from reachy_sage_voice import beat, dance

        await self._home()
        start = self.clock()
        while not beat.music_present(pcm := self.audio(dance.WINDOW_S)):
            if self.clock() - start >= dance.LISTEN_S - dance.TICK_S:
                return logger.info("dance: no beat heard")
            await asyncio.sleep(dance.ESTIMATE_S)
        lead = float(os.environ.get("SAGE_VOICE_DANCE_LEAD_S", dance.DEFAULT_LEAD_S))
        groove, t0 = dance.Groove(pcm, self.clock(), lead), self.clock()

        def at(s: float) -> dict:
            t = t0 + s
            if self._held.is_set() and t - self._held_at < self.hold_max_s:
                groove.last = t  # the beat clock stands still while the person speaks
                return dance.pose(groove.current)
            groove.advance(t)
            return dance.pose(groove.at(t))
        playing = asyncio.ensure_future(self._play(Sample(at, t0, math.inf)))
        try:
            misses = 0
            while misses < dance.MISSES:
                await asyncio.wait({playing}, timeout=dance.ESTIMATE_S)
                if playing.done():
                    return playing.result()  # the link failed under it
                if beat.music_present(pcm := self.audio(dance.WINDOW_S)):
                    misses = 0
                    groove.hear(pcm, self.clock())
                else:
                    misses += 1
        finally:
            playing.cancel()

    async def _search_face(self, step: Step) -> str:
        """Find the owner: voice first, call out, give up by the cap (``behaviour/search.py``).

        Seeded with the voice's direction and where a face was last seen. The body moves
        to each view, settles and reads the daemon's face through ``dwell_s`` (live
        2026-10-23: a continuous sweep passed the face); a face seen even mid-move is
        locked on once seen again within ``confirm_s``. A call-out is said by
        ``call_out`` and listened to for ``listen_s``. The person speaking holds it
        (at most ``hold_max_s``); that time counts against the cap (owner, 2026-10-25).
        With no face, the body goes home and the search fails "not_found" ("timeout"
        when the step gave its own limit).
        """
        search = replace(self.search, cap_s=step.timeout_s) if step.timeout_s else self.search
        here = head_yaw(await self._call("get_current_head_pose"))
        last = here if self._locked else self._last_seen  # tracking: the face is where it looks
        self._heard = False
        state = search.start(await self._voice(), last, self.clock())
        logger.info("search: voice %s, last seen %s, %.0f s to look", state.seeds, last,
                    search.cap_s)
        await self._weight(SEARCH_WEIGHT)
        seen, views = False, 0
        try:
            while True:
                heard, self._heard = self._heard, False
                state, act = search.next_step(state, self.clock(), seen,
                                              await self._voice() if heard else None)
                if isinstance(act, Found):
                    break
                if isinstance(act, GiveUp):
                    await self._home()  # the owner: it stays at the default pose
                    raise _FailedError("timeout" if step.timeout_s else "not_found",
                                       f"no face in {views} views in {search.cap_s:.0f} s")
                if isinstance(act, CallOut):
                    logger.info("search: calling out")
                    if self.call_out is not None:
                        await self.call_out()
                    await asyncio.sleep(search.listen_s)
                    continue
                views += 1
                logger.info("search view %d: yaw %.0f", views, act.bearing)
                seen = (await self._view(act.bearing, -act.pitch)
                        or await self._watch(search.dwell_s)) and await self._confirm()
        except _FailedError as failure:
            logger.info("search outcome: %s (%s)", failure.reason, failure.text)
            await self._release()
            raise
        finally:
            if self._primary is self._dwell:  # the view is over: tracking or home has the head
                self._primary = None
        logger.info("search outcome: found")
        self._last_seen = head_yaw(await self._call("get_current_head_pose"))
        await self._lock()
        return "found you; now tracking you"

    async def _voice(self) -> float | None:
        """Return the voice's world yaw in degrees, left positive, or None."""
        angle = await self._doa()
        return None if angle is None else math.degrees(await self._yaw()) + 90 - math.degrees(angle)

    async def _view(self, yaw: float, elevation: float) -> bool:
        """Move to a view; True if a face is seen on the way.

        Body and head turn the same way, so their angles add (owner, 2026-10-23:
        counter-rotating cut the path short): the body goes as far as it reaches
        and the head, in the world frame, the rest of the way. Head and body are
        moved together in steps of at most ``VIEW_STEP_DEG``.
        """
        here_body = math.degrees(await self._yaw())
        here_head = head_yaw(await self._call("get_current_head_pose"))
        body = max(-MAX_BODY, min(MAX_BODY, yaw))
        span = max(abs(body - here_body), abs(yaw - here_head))
        count = max(1, math.ceil(span / VIEW_STEP_DEG - 1e-9))
        for i in range(1, count + 1):
            at_body = here_body + (body - here_body) * i / count
            at_head = here_head + (yaw - here_head) * i / count
            head = create_head_pose(yaw=at_head, pitch=-elevation)  # SDK pitch: down
            if not self.arbiter:
                await self._segment(head=head, body_yaw=math.radians(at_body))
            else:  # the runner holds the view through the dwell, breathing under it
                self._primary = self._dwell = Hold(
                    await self._goto(head=head, body_yaw=math.radians(at_body)))
                self._run_motion()
            if await self._seen():
                return True
        await asyncio.sleep(self.timing.settle_s)
        return False

    async def _confirm(self) -> bool:
        return await self._watch(self.timing.confirm_s)

    async def _watch(self, seconds: float) -> bool:
        """Read the face every ``poll_s`` for ``seconds``; True at the first one seen."""
        end = time.monotonic() + seconds
        while True:
            await asyncio.sleep(self.timing.poll_s)
            if await self._seen():
                return True
            if time.monotonic() >= end:
                return False

    async def _seen(self) -> bool:
        seen = await self._face()
        logger.info("search: face read %s", "seen" if seen else "none")
        return seen

    async def _emote(self, step: Step) -> str:
        return await self._recorded("emotion", expressions.emotion(step.arg), step.arg)

    async def _dance_move(self, step: Step) -> str:
        return await self._recorded("dance", expressions.dance(step.arg), step.arg)

    async def _recorded(self, kind: str, name: str | None, asked: str) -> str:
        """Play a recorded move as an overlay (with the arbiter, a Sample): stop cuts it."""
        player = self.clip if self.arbiter else self.play_move
        if player is None or name is None:
            raise _FailedError("unsupported", f"no recorded {kind} for {asked}")
        logger.info("recorded %s: %s (asked for %s)", kind, name, asked)
        await self.overlay(lambda: self._sample(kind, name) if self.arbiter
                           else self.play_move(kind, name))
        return f"played {name}"

    async def _stop(self, step: Step) -> str:
        self._halt()
        await self._freeze()
        return "stopped"

    # --- tracking -------------------------------------------------------------

    async def _lock(self) -> None:
        self._locked = True
        await self._weight(1.0)

    async def _release(self) -> None:
        """End a search's tracking: back to locked-on tracking if it was, else none."""
        if self._locked:
            await self._weight(1.0)
        else:
            await self._untrack()

    async def _weight(self, weight: float) -> None:
        if self._tracking != weight:
            if weight < (self._tracking or 0.0):
                await self._hand_back()
            self._tracking = weight
            self._start_ramp()
            await self._call("start_head_tracking", weight=self._sent_weight())

    async def _untrack(self) -> None:
        if self._tracking is not None:
            await self._hand_back()
            self._tracking = None
            self._end_ramp()
            await self._call("stop_head_tracking")

    def _steered(self) -> bool:
        """Whether the tracker has the head, not the target last sent.

        ponytail: at SEARCH_WEIGHT the head is the target's, but for 5% of the way to a face, a
        step of 2 degrees at most on letting go: a search's stop keeps its soft brake for it.
        """
        return (self._sent_weight() or 0.0) > SEARCH_WEIGHT

    async def _hand_back(self) -> None:
        """Make where the head is the target, at rest, before the tracker lets go of it.

        While it steers, the daemon stores our targets without applying them (abstract.py
        set_target_head_pose), and letting go makes the last one live at motor speed
        (clear_tracking_aim). The runner then slews from here. Its next ticks are from here
        too, so no stale target comes after this one (one worker, in order).
        """
        if not self._steered():
            return
        try:
            here = runner.pose(*await self._pose())
        except Exception as error:  # the weight's own call says the link is down
            return logger.warning("could not read the head to hand it back: %s", error)
        if self._step:
            self._step.rebase(here, self.clock())
            self._state, self._sent = self._step.state, here
        if self._primary is self._idling:
            self._primary = self._idling = Hold(here)
        await self._send(here)

    def _start_ramp(self) -> None:
        """Raise a weight above SEARCH_WEIGHT from there, with the arbiter (``_ramp_up``)."""
        self._end_ramp()
        if self.arbiter and TRACK_RAMP_S > 0 and (self._tracking or 0.0) > SEARCH_WEIGHT:
            self._ramp = SEARCH_WEIGHT
            self._ramping = asyncio.ensure_future(self._ramp_up())

    def _end_ramp(self) -> None:
        if self._ramping:
            self._ramping.cancel()
        self._ramp, self._ramping = 1.0, None

    async def _ramp_up(self) -> None:
        """Wait for a face, then raise the weight: the head blends from its target to the aim.

        The face first, as the daemon latches a weight only on seeing one: a ramp run with nobody
        there would end with the first face taken at weight 1.
        """
        while True:
            with contextlib.suppress(Exception):  # no reading: no face yet
                if await self._face():
                    break
            await asyncio.sleep(RAMP_POLL_S)
        for i in range(1, RAMP_STEPS + 1):
            self._ramp = SEARCH_WEIGHT + (1.0 - SEARCH_WEIGHT) * i / RAMP_STEPS
            await self._reflex_weight()
            await asyncio.sleep(TRACK_RAMP_S / RAMP_STEPS)

    # --- robot ----------------------------------------------------------------

    async def _doa(self) -> float | None:
        try:
            return await asyncio.to_thread(self.doa)  # an HTTP read: off the event loop
        except Exception as error:
            logger.warning("no direction of arrival: %s", error)
            return None

    async def _sweep_to(self, here: float, target: float, deadline: float | None = None) -> bool:
        """Turn the body in segments, the head with it; with a ``deadline``, watch for a face.

        Head poses are in the world frame, and with the SDK's automatic body yaw a
        head left behind drags the body back within 65° of it: so the head goes too.
        Returns True once a face is seen (only when watching).
        """
        if math.isclose(target, here, abs_tol=1e-3):
            return False
        count = math.ceil(abs(target - here) / self.step)
        for i in range(1, count + 1):
            if deadline is not None and self._moving_s() >= deadline:
                raise _FailedError("not_found", "no face found in time")
            body = here + (target - here) * i / count
            await self._goto(head=create_head_pose(yaw=math.degrees(body)), body_yaw=body)
            if deadline is not None and await self._face():
                return True
        return False

    async def home(self) -> None:
        """Go back to the default pose, unless locked on (tracking owns the head) or busy."""
        if not self._locked and self._plan is None and self._activity is None:
            await self._home()

    async def _home(self) -> None:
        """Go to the SDK's init pose and body yaw 0 (research: about 1 s of goto)."""
        here = await self._yaw()
        near = max(-self.step, min(self.step, here))
        await self._sweep_to(here, near)  # in segments from far round: stop lands within one
        await self._goto(head=INIT_HEAD_POSE, antennas=INIT_ANTENNAS_JOINT_POSITIONS,
                         body_yaw=0.0)

    def _halt(self) -> None:
        """End moves in flight, at once, beside a worker blocked in a goto.

        ``cancel_move`` ends an SDK-side move (an emote) at its next frame; the
        daemon's ``stop_move`` ends a daemon-run one (a goto, a recorded move) and
        its sound, which cancel_move cannot (research, 2026-10-23). With the arbiter there is
        neither: the runner is the only writer, and stop has cleared its primary. Only an
        emote's sound is left, which cancel_move stopped (media.stop_playing).
        """
        if self.arbiter:
            try:
                self.mini.media.stop_playing()
            except Exception as error:  # media mid-reset: the freeze still follows
                logger.warning("could not stop the emote's sound: %s", error)
            return
        if hasattr(self.mini, "cancel_move"):
            self.mini.cancel_move()
        try:
            self.mini.client.send_command(StopMoveCmd())
        except Exception as error:  # a fake, or a link already gone: the freeze still follows
            logger.warning("could not send stop_move: %s", error)

    async def _until_free(self) -> None:
        """Wait while the person speaks, at most ``hold_max_s`` from when they started."""
        if not self._free.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._free.wait(),
                                       self._held_at + self.hold_max_s - self.clock())

    async def _segment(self, **target) -> None:
        await self._until_free()
        async with self._head:
            try:
                await self._call("goto_target", duration=self.segment_s, **target)
            finally:
                self._moved_at = self.clock()

    def _braking(self) -> Hold | None:
        """With the runner writing, a Hold where its slewed target can brake to (a soft stop).

        ponytail: only while the runner wrote within 0.1 s and tracking has not the head; else
        another writer had the body and a read is the truth.
        """
        if not self._steered() and self._sent is not None and (
                self.clock() - self._ticked < 0.1 or self._down_since is not None):  # no ticks, no read
            return Hold(arbiter.stopping(self._state, self.speed))
        return None

    async def _freeze(self, hold: Hold | None = None) -> None:
        """Hold where the body is: with the arbiter, where the runner can brake to (``hold``)."""
        if self.arbiter:
            await self._hand_back()
            return await self._play(hold or self._braking()
                                    or Hold(runner.pose(*await self._pose())))
        head = await self._call("get_current_head_pose")
        joints, _ = await self._call("get_current_joint_positions")
        await self._call("set_target", head=head, body_yaw=joints[0])

    async def _yaw(self) -> float:
        joints, _ = await self._call("get_current_joint_positions")
        return joints[0]  # body yaw is the first head joint

    async def _face(self) -> bool:
        """Whether a fresh face is seen: a stale or detector-off reading never counts (F1)."""
        face: Face | None = None
        if self.face is not None:
            face = await asyncio.to_thread(self.face)  # an HTTP read: off the event loop
        if face is None:
            reading = await self._call("get_tracked_face", wait=False)
            on = bool(self._tracking) and not getattr(self.mini, "refused", False)
            face = face_percept(reading, time.monotonic(), on)
        return face.detected

    async def _reflex(self, name: str, **kwargs) -> None:
        """Make a robot call an emote can do without: a failure is logged, the emote goes on."""
        try:
            await self._call(name, **kwargs)
        except Exception as error:
            logger.warning("%s failed (%s) - carrying on", name, error)

    async def _call(self, name: str, **kwargs):
        if name in ("goto_target", "set_target"):
            kwargs = within_limits(kwargs)
        method = functools.partial(getattr(self.mini, name), **kwargs)
        try:
            result = await asyncio.get_running_loop().run_in_executor(  # context: M1's marker
                self._robot, contextvars.copy_context().run, method)
        except ConnectionError as error:
            self._link_down(error)
            raise
        if name in _COMMANDS:
            self._link_up()
        return result

    def _link_down(self, error: Exception) -> None:
        """Log a refused command as the link going down, once; report it dead after a while."""
        now = self.clock()
        if self._down_since is None:
            self._down_since, self._stalls = now, self._stalls + 1
            logger.warning("motion link down (%s): commands refused, idle motion paused "
                           "(stall %d)", error, self._stalls)
            self.on_link(False, self._stalls)
        elif now - self._down_since >= MOTION_DEAD_S and not self._dead:
            self._dead = True
            self.link_lost(f"the robot's motion link: no command accepted for "
                           f"{now - self._down_since:.0f} s ({error})")

    def _link_up(self) -> None:
        if self._down_since is not None:
            logger.info("motion link back after %.1f s (%d stalls so far)",
                        self.clock() - self._down_since, self._stalls)
            self._down_since, self._dead = None, False
            self.on_link(True, self._stalls)
            # The cached weight may be one the stalled link refused: say it again (LAYERS F1).
            self._reassert = asyncio.ensure_future(self._reflex_weight())

    def _sent_weight(self) -> float | None:
        """Return the weight to send: the desired one as far as it came up; 0 for a Sample."""
        if self._tracking is None:
            return None
        return 0.0 if self._dropped and self._tracking else min(self._tracking, self._ramp)

    async def _reflex_weight(self) -> None:
        if self._tracking is not None:
            await self._reflex("start_head_tracking", weight=self._sent_weight())
