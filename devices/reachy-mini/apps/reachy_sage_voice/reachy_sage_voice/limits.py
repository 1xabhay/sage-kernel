"""The body's joint limits, in degrees, and the head's height in metres.

The one table every target is clamped to.

The daemon silently drops a target past a limit, which reads as a freeze (eval A5). Each
limit is min(the SDK's clamp, the measured reach) less 2 degrees (docs/design/LAYERS.md §0).
Measured with robot/calibration/reach_probe.py, body and head turned together: the head
looks to +173 left and -157 right, and turns about 55 off the body. The SDK clamps the body
at 160 and the head at 65 off it. Re-run the probe after a firmware or daemon update, and
date the new numbers.
"""

MEASURED = "2026-10-23"
LIMITS = {
    "body_yaw": (-158.0, 158.0),  # the SDK's max_body_yaw, 160
    "yaw": (-155.0, 171.0),  # where the head looks, in the world: measured -157 / +173
    "head_yaw": (-53.0, 53.0),  # the head off the body: measured 55, inside the SDK's 65
    "pitch": (-40.0, 40.0),  # Pollen's docs
    "roll": (-40.0, 40.0),
    # ponytail: the SDK documents no head translation limit; 10 mm keeps a scale-10 breath
    # (50 mm) in reach. Measure it with the reach probe and set it here.
    "z": (-0.010, 0.010),
}
#: Body yaw commanded per degree wanted. The body undershoots by 3-5 degrees (owner,
#: 2026-10-23). ponytail: 1.0 until the probe measures the gain; then set it here.
BODY_GAIN = 1.0


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def clamp(pose: dict[str, float]) -> dict[str, float]:
    """Return ``pose`` inside ``LIMITS``: body_yaw, and the head's yaw, pitch and roll in the world.

    The body is commanded at ``BODY_GAIN``. With the body in the pose, the head also
    stays within ``head_yaw`` of it. Other keys pass through.
    """
    out = {k: _clip(v, *LIMITS[k]) if k in LIMITS else v for k, v in pose.items()}
    if "body_yaw" in pose:
        out["body_yaw"] = _clip(pose["body_yaw"] * BODY_GAIN, *LIMITS["body_yaw"])
        if "yaw" in pose:
            low, high = (out["body_yaw"] + off for off in LIMITS["head_yaw"])
            out["yaw"] = _clip(_clip(pose["yaw"], low, high), *LIMITS["yaw"])
    return out
