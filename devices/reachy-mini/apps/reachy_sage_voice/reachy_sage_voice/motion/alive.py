"""Aliveness offsets as pure functions of time: breathing, antennas and slow drift.

Breathing is Pollen's (reachy_mini_conversation_app, moves.py): 5 mm of z at 0.1 Hz, the
antennas 15 degrees at 0.5 Hz in opposition. The drift is three incommensurate sines per
channel, so it never reads as a metronome (03-external-research §3). Everything fades in
``FADE_AFTER_S`` after primary motion ends. The antennas run on their own clock, which the
caller stops while the owner speaks (the listening freeze) and resumes, so they never snap.
``scale`` means what SAGE_VOICE_IDLE_SCALE means today (executor.idle_scale): a multiplier of
every amplitude, 0 still to 10. ponytail: amplitudes are first guesses, to tune live.
"""

import math

FADE_AFTER_S = 0.3  # quiet this long after a move before aliveness starts
FADE_S = 1.0  # from nothing to full
BREATH_Z_M, BREATH_HZ = 0.005, 0.1
ANTENNA_DEG, ANTENNA_HZ = 15.0, 0.5
#: Drift: degrees, then three periods in seconds with irrational ratios (1, sqrt 2, golden).
DRIFT = {"yaw": (10.0, (11.0, 11.0 * math.sqrt(2), 11.0 * (1 + math.sqrt(5)) / 2)),
         "pitch": (4.0, (7.0, 7.0 * math.sqrt(3), 7.0 * math.e)),
         "roll": (3.0, (9.0, 9.0 * math.sqrt(5), 9.0 * math.pi / 2))}
DRIFT_WEIGHTS = (0.55, 0.3, 0.15)
BODY_SHARE = 0.4  # the body takes this much of the yaw drift, same sign: no counter-rotation
# The listening reflex: antennas at a voice's first frame, a nod at a pause (aiming to be seen
# 160-200 ms after the onset, 310-350 ms after the last voice). Inside the caps: 125 deg/s, 1280
# deg/s^2 on the antennas, 20 deg/s, 150 deg/s^2 on the nod. ponytail: guesses to tune live.
PERK_DEG, PERK_RISE_S, PERK_FALL_S = 20.0, 0.3, 1.2
NOD_DEG, NOD_S = 5.0, 0.8
_LANDING_S = 0.05


def fade(still_s: float) -> float:
    """0 until ``FADE_AFTER_S`` after a move, then a smoothstep up to 1 over ``FADE_S``."""
    u = max(0.0, min(1.0, (still_s - FADE_AFTER_S) / FADE_S))
    return u * u * (3 - 2 * u)


def _min_jerk(u: float) -> float:
    u = max(0.0, min(1.0, u))
    return u ** 3 * (10 - 15 * u + 6 * u * u)


def listening(perk_s: float | None, quiet_s: float | None, nod_s: float | None) -> dict:
    """Return the listening reflex's offset, seconds since the lift, the voice's end, and the nod.

    The antennas lift together (left +, right -: toward upright, the SDK's sleep is -3, +3) and
    stay up until the voice goes; the nod is one slow dip. None: not happening.
    """
    pose = {}
    if perk_s is not None and (quiet_s is None or quiet_s < PERK_FALL_S):
        up = PERK_DEG * _min_jerk(perk_s / PERK_RISE_S)
        if quiet_s is not None:
            up *= 1 - _min_jerk(quiet_s / PERK_FALL_S)
        pose |= {"left": up, "right": -up}
    if nod_s is not None and nod_s < NOD_S:
        pose["pitch"] = NOD_DEG * math.sin(math.pi * nod_s / NOD_S) ** 2  # + is down
    return pose


def listening_moves(perk_s: float | None, quiet_s: float | None, nod_s: float | None) -> bool:
    """Whether the reflex is still moving: risen and waiting on the voice is not.

    A tick or two past its end, so its end is what the runner last sends.
    """
    return ((perk_s is not None and (perk_s < PERK_RISE_S + _LANDING_S or (
                quiet_s is not None and quiet_s < PERK_FALL_S + _LANDING_S)))
            or (nod_s is not None and nod_s < NOD_S + _LANDING_S))


def _wave(t: float, hz: float) -> float:
    return math.sin(2 * math.pi * hz * t)


def offsets(t: float, still_s: float, antenna_t: float, scale: float = 1.0) -> tuple[dict, ...]:
    """Return the breathing, antenna and drift offsets at ``t``, for ``arbiter.tick``.

    ``still_s`` is ``arbiter.still_s``; ``antenna_t`` the antenna clock, stopped while the owner
    speaks.
    """
    k = scale * fade(still_s)
    breath = {"z": k * BREATH_Z_M * _wave(t, BREATH_HZ)}
    left = k * ANTENNA_DEG * _wave(antenna_t, ANTENNA_HZ)
    drift = {key: k * amp * sum(w * _wave(t, 1 / period)
                                for w, period in zip(DRIFT_WEIGHTS, periods, strict=True))
             for key, (amp, periods) in DRIFT.items()}
    drift["body_yaw"] = BODY_SHARE * drift["yaw"]
    return breath, {"left": left, "right": -left}, drift
