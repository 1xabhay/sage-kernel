"""Dance to music from the speakers: Pollen's dance moves, phase-locked to the beat the mic hears.

- Goes home, then listens up to ``LISTEN_S`` for a beat (``beat.py``), else
  fails ``no_music``.
- The moves are Pollen's ``reachy_mini_dances_library``: each is a function of
  musical time, ``move(t_beats, **params)`` -> head and antenna offsets. Every
  50 Hz tick evaluates the current move at the phase of a phase-locked loop
  and sends it with ``set_target``, with the body swaying left one bar and
  right the next under it. The move changes every ``SWITCH_BEATS``.
- The loop (after binhpham's groove.py, Apache-2.0, see NOTICE): the phase, in
  beats, advances by ``dt / period`` each tick and is pulled toward the heard
  grid with time constant ``PLL_TAU_S``, so a new tempo read never makes it
  jump. ``SAGE_VOICE_DANCE_LEAD_S`` (default 0.2 s) runs it ahead of what the
  mic heard: speakers, room, robot mic, WebRTC, the Mac and the motors are
  ~150-300 ms behind the music. Calibrate it live with a click track.
- Nothing jumps: a move change, the end of a hold or a flourish blends from
  where the head is over ``BLEND_BEATS``; loudness changes ease in over 1 s.
- Tempo is re-read every ``ESTIMATE_S`` on the last ``WINDOW_S``; two reads in
  a row without music (silence, or a fade-out) end it ``done``, as does the
  timeout. On a drop, at most every ``COOLDOWN_S``, it eases home and plays one
  recorded emotion from ``moves``, muted, then blends back into the groove.
- ``hold`` set: frozen where it is, no targets sent, the beat clock still
  running; cleared: resumes in phase, blended (owner, 2026-10-23: motion
  ruins the STT).
- ``stop`` is seen within one tick; a flourish is cancelled at its next frame.
  Every exit goes home: INIT_HEAD_POSE, body yaw 0, antennas at rest.

For the caller: face tracking at weight 1 overrides every set_target, so
stop or lower it first; set_target is ignored while a daemon move (goto,
recorded dataset move) runs; flourishes play SDK-side, so ``cancel_move``
ends them (a daemon-side move would need ``stop_move``). Never uses the camera.

Ranges, against the robot's limits (SDK and live robot, 2026-10-23), at full loudness:
the library's moves tilt the head up to 25° (cone 35°; clamped there),
turn it up to 45° from the body (±55°), and move it up to 40 mm; the body
sways ±30° (±160°); antennas swing up to 60° from rest (±180°).
Left out: headbanger_combo (30° of tilt), and the four that snap, jumping
6-40° in an instant whatever the tempo: yeah_nod, chicken_peck, grid_snap,
jackson_square.
"""

import asyncio
import math
import os
import time

import numpy as np
from reachy_mini.utils import create_head_pose
from reachy_mini_dances_library.collection.dance import AVAILABLE_MOVES

from reachy_sage_voice import beat
from reachy_sage_voice.motion import runner

TICK_S = 0.02  # 50 Hz targets
WINDOW_S = 6.0  # audio per tempo read
ESTIMATE_S = 1.0  # between reads
LISTEN_S = 8.0  # to find a beat before giving up
MISSES = 2  # reads in a row without music that end the dance
PLL_TAU_S = 0.5
DEFAULT_LEAD_S = 0.2
SWITCH_BEATS = 16  # four bars per move
BLEND_BEATS = 1.0
LEVEL_TAU_S = 1.0
WARMUP_S = 8.0  # dancing this long before a drop counts
COOLDOWN_S = 20.0  # between flourishes
HOME_S = 0.6
BODY_DEG = 30.0
MAX_TILT_DEG = 25.0
REST_ANTENNAS = np.array([-0.1745, 0.1745])  # the SDK's INIT_ANTENNAS_JOINT_POSITIONS
#: The library's moves, in the order danced. ponytail: a fixed order; pick by energy if it bores.
ROTATION = ("groovy_sway_and_roll", "simple_nod", "side_to_side_sway", "uh_huh_tilt",
            "head_tilt_roll", "pendulum_swing", "side_glance_flick", "chin_lead",
            "sharp_side_tilt", "polyrhythm_combo", "neck_recoil", "dizzy_spin", "side_peekaboo",
            "stumble_and_recover", "interwoven_spirals")
#: Recorded emotions a drop may set off, in turn; those missing from ``moves`` are skipped.
FLOURISHES = ("enthusiastic1", "electric1", "dance1", "success1", "dance2", "enthusiastic2",
              "cheerful1", "dance3", "success2", "laughing1")
NO_MUSIC = ("failed", "no_music", "I can't hear a beat")
_STILL = np.zeros(9)  # offsets: x, y, z (m), roll, pitch, yaw (rad), 2 antennas (rad), body yaw


def offsets(name: str, phase: float, level: float) -> np.ndarray:
    """Move ``name`` at ``phase`` (beats) as an offset vector, body sway included, at ``level``."""
    fn, params, _ = AVAILABLE_MOVES[name]
    o = fn(phase, **params)
    body = math.radians(BODY_DEG) * math.sin(math.pi * phase / 4)
    return level * np.concatenate([o.position_offset, o.orientation_offset, o.antennas_offset,
                                   [body]])


def target(v: np.ndarray):
    """Head, antennas and body yaw for offset vector ``v``; the move is in the body's frame."""
    x, y, z, roll, pitch, yaw, a, b, body = v
    tilt, limit = math.hypot(roll, pitch), math.radians(MAX_TILT_DEG)  # bounds the true tilt
    if tilt > limit:
        roll, pitch = roll * limit / tilt, pitch * limit / tilt
    head = create_head_pose(yaw=body, degrees=False) @ create_head_pose(
        x=x, y=y, z=z, roll=roll, pitch=pitch, yaw=yaw, degrees=False)
    return head, list(REST_ANTENNAS + [a, b]), float(body)  # rest plus the move's offset


async def dance(mini, audio, stop: asyncio.Event, *, timeout_s: float = 180, clock=time.monotonic,
                sleep=asyncio.sleep, moves=None, hold: asyncio.Event | None = None,
                ) -> tuple[str, str | None, str]:
    """Dance to what ``audio(seconds)`` hears until it stops, ``stop`` is set, or ``timeout_s``.

    Returns ``(status, reason, text)``; see the module notes.
    """
    lead = float(os.environ.get("SAGE_VOICE_DANCE_LEAD_S", DEFAULT_LEAD_S))
    start = clock()
    try:
        await _home(mini)
        while True:
            if stop.is_set():
                return ("interrupted", None, "stopped before I found the beat")
            pcm = audio(WINDOW_S)
            if beat.music_present(pcm):
                break
            if clock() - start >= LISTEN_S - TICK_S:  # the last read lands on LISTEN_S
                return NO_MUSIC
            await _wait(stop, sleep, ESTIMATE_S)
        return await _groove(mini, audio, stop, hold, pcm, start, timeout_s, clock, sleep, lead,
                             [moves[n] for n in FLOURISHES if moves and n in moves])
    finally:
        await _home(mini)


class Groove:
    """The beat clock and the moves on it, a function of time: the dance without its I/O."""

    def __init__(self, pcm: bytes, now: float, lead: float):
        """Start on the beat heard in ``pcm`` at ``now``, ``lead`` s ahead of it."""
        self.lead, self.last, self.move, self.count, self.due = lead, now, 0, 0, None
        self.hear(pcm, now)
        self.level = self.goal
        self.phase = (now + lead - self.anchor) / self.period
        self.counted = math.floor(self.phase)
        self.blend_from, self.blend_at, self.current = _STILL, now, _STILL

    def hear(self, pcm: bytes, now: float) -> None:
        """Take the tempo, the beat grid and the loudness from music heard at ``now``."""
        self.bpm, since, _ = beat.tempo(pcm)
        self.period, self.anchor, self.goal = 60 / self.bpm, now - since, _level(pcm)

    def advance(self, now: float) -> None:
        """Run the loop on to ``now``: predict, then pull toward the heard grid; never jump."""
        dt, self.last = now - self.last, now
        self.phase += dt / self.period
        error = ((now + self.lead - self.anchor) / self.period - self.phase + 0.5) % 1.0 - 0.5
        self.phase += error * min(1.0, dt / PLL_TAU_S)
        self.level += (self.goal - self.level) * min(1.0, dt / LEVEL_TAU_S)
        if math.floor(self.phase) != self.counted:
            self.counted, self.count = math.floor(self.phase), self.count + 1
            if self.count >= SWITCH_BEATS and self.due is None:
                self.move, self.count = (self.move + 1) % len(ROTATION), 0
                self.blend(now)

    def blend(self, now: float) -> None:
        """Ease from where the head is, from ``now``."""
        self.blend_from, self.blend_at = self.current, now

    def weight(self, now: float) -> float:
        """How far the blend is, 0 to 1."""
        return min(1.0, (now - self.blend_at) / (BLEND_BEATS * self.period))

    def at(self, now: float) -> np.ndarray:
        """Return the offsets at ``now``: the move at the phase (still while a flourish is due)."""
        w = self.weight(now)
        live = _STILL if self.due is not None else offsets(ROTATION[self.move], self.phase,
                                                            self.level)
        self.current = self.blend_from + (live - self.blend_from) * (3 * w * w - 2 * w ** 3)
        return self.current


def pose(v: np.ndarray) -> dict:
    """Return the motion runner's pose for offset vector ``v`` (the arbiter's dance)."""
    head, _, body_yaw = target(v)
    return runner.pose(head, body_yaw, REST_ANTENNAS + v[6:8])


async def _groove(mini, audio, stop, hold, pcm, start, timeout_s, clock, sleep, lead, flourishes):
    g = Groove(pcm, clock(), lead)
    danced = g.last
    next_read, misses, flourished, held = danced + ESTIMATE_S, 0, -math.inf, False
    while True:
        now = clock()
        if stop.is_set():
            return ("interrupted", None, f"stopped dancing at ~{round(g.bpm)} bpm")
        if now - start >= timeout_s:
            break
        if now >= next_read:
            next_read = now + ESTIMATE_S
            pcm = audio(WINDOW_S)
            if not beat.music_present(pcm):
                misses += 1
                if misses >= MISSES:
                    break
            else:
                misses = 0
                g.hear(pcm, now)
                if (beat.drop(pcm) and flourishes and g.due is None and now - danced >= WARMUP_S
                        and now - flourished >= COOLDOWN_S):
                    g.due = flourishes[0]
                    g.blend(now)  # ease home first
        g.advance(now)

        if hold is not None and hold.is_set():
            held = True
            await sleep(TICK_S)
            continue
        if held:
            held = False
            g.blend(now)
        if g.due is not None and g.weight(now) >= 1.0:
            if not await _flourish(mini, g.due, stop):
                return ("interrupted", None, f"stopped dancing at ~{round(g.bpm)} bpm")
            flourishes.append(flourishes.pop(0))
            g.due, flourished, g.count = None, clock(), 0
            g.blend_from, g.blend_at, g.current = _STILL, clock(), _STILL
            g.last = clock()
            continue
        head, antennas, body_yaw = target(g.at(now))
        mini.set_target(head=head, antennas=antennas, body_yaw=body_yaw)
        await sleep(TICK_S)
    return ("done", None, f"danced to ~{round(g.bpm)} bpm")


def _level(pcm: bytes) -> float:
    # ponytail: -50..-25 dBFS -> 0.6..1, a guess; set it from music at the robot's mic.
    return float(np.clip((beat.dbfs(pcm) + 50) / 25, 0.6, 1.0))


async def _home(mini) -> None:
    await asyncio.to_thread(mini.goto_target, np.eye(4), antennas=list(REST_ANTENNAS),
                            duration=HOME_S, body_yaw=0.0)


async def _wait(stop: asyncio.Event, sleep, seconds: float) -> None:
    """Sleep ``seconds`` in ticks, or until ``stop`` is set."""
    for _ in range(round(seconds / TICK_S)):
        if stop.is_set():
            return
        await sleep(TICK_S)


async def _flourish(mini, move, stop: asyncio.Event) -> bool:
    """Play ``move`` muted; False if ``stop`` cut it short (cancelled at its next frame)."""
    play = asyncio.ensure_future(mini.async_play_move(move, sound=False))
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({play, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if not play.done():
            mini.cancel_move()
        await play
        return not stop.is_set()
    finally:
        stopped.cancel()
        if not play.done():  # the dance itself was cancelled: the move must not play on
            mini.cancel_move()
